//! UDP receiver + bounded ring buffer for IF I WIFI.
//!
//! Deliberately uses `std::net` (blocking sockets) rather than tokio. One
//! capture thread reads frames into a fixed-size ring; the consumer drains
//! the ring on its own cadence. If the consumer falls behind, we drop the
//! oldest frame and bump a counter rather than blocking the network read.
//!
//! This is enough for a single ESP32-S3 at 50 Hz on a LAN. If you ever
//! need multi-node high-throughput aggregation, swap this for a proper
//! event-loop receiver and benchmark it.

use std::net::{SocketAddr, UdpSocket};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use ifi_core::{decode, CsiFrame, DecodeError};

/// Max single-frame size on the wire. Matches the firmware's
/// `CSI_MAX_FRAME_SIZE` (header + 4 antennas × 256 subcarriers × 2 bytes).
const MAX_FRAME: usize = 20 + 4 * 256 * 2;

/// Stats counters maintained by the receiver thread.
#[derive(Debug, Clone, Copy, Default)]
pub struct RxStats {
    pub frames_ok: u64,
    pub frames_bad: u64,
    pub frames_dropped_full: u64,
    pub sync_packets: u64,
}

#[derive(Debug)]
pub enum RxError {
    Io(std::io::Error),
}

impl From<std::io::Error> for RxError {
    fn from(e: std::io::Error) -> Self {
        RxError::Io(e)
    }
}

impl std::fmt::Display for RxError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RxError::Io(e) => write!(f, "I/O error: {}", e),
        }
    }
}

impl std::error::Error for RxError {}

struct Shared {
    queue: Mutex<RingBuf>,
    stats: Mutex<RxStats>,
}

/// Bounded ring buffer. We don't use `std::collections::VecDeque` directly
/// because we want explicit "drop oldest on overflow" semantics and an
/// explicit capacity that the caller chose.
struct RingBuf {
    items: Vec<CsiFrame>,
    cap: usize,
}

impl RingBuf {
    fn new(cap: usize) -> Self {
        Self {
            items: Vec::with_capacity(cap),
            cap,
        }
    }
    /// Push, dropping the oldest if full. Returns true if a drop happened.
    fn push(&mut self, f: CsiFrame) -> bool {
        let dropped = self.items.len() >= self.cap;
        if dropped {
            self.items.remove(0);
        }
        self.items.push(f);
        dropped
    }
    fn drain(&mut self) -> Vec<CsiFrame> {
        std::mem::take(&mut self.items)
    }
}

/// Handle to a running UDP receiver.
pub struct Receiver {
    shared: Arc<Shared>,
    _thread: JoinHandle<()>,
}

impl Receiver {
    /// Bind a UDP socket on `addr` and start a background receive thread.
    /// `capacity` is the max number of frames buffered before we start
    /// dropping the oldest.
    pub fn bind(addr: SocketAddr, capacity: usize) -> Result<Self, RxError> {
        let socket = UdpSocket::bind(addr)?;
        // Set a short read timeout so the thread can exit cleanly if we
        // ever add a shutdown signal in the future.
        socket.set_read_timeout(Some(Duration::from_millis(500)))?;

        let shared = Arc::new(Shared {
            queue: Mutex::new(RingBuf::new(capacity)),
            stats: Mutex::new(RxStats::default()),
        });

        let s = Arc::clone(&shared);
        let handle = thread::Builder::new()
            .name("ifi-udp-rx".into())
            .spawn(move || rx_loop(socket, s))
            .expect("spawn rx thread");

        Ok(Self {
            shared,
            _thread: handle,
        })
    }

    /// Drain everything received since the last call. Returns the frames
    /// in arrival order. Non-blocking.
    pub fn drain(&self) -> Vec<CsiFrame> {
        self.shared.queue.lock().unwrap().drain()
    }

    /// Snapshot of the lifetime counters.
    pub fn stats(&self) -> RxStats {
        *self.shared.stats.lock().unwrap()
    }
}

fn rx_loop(socket: UdpSocket, shared: Arc<Shared>) {
    let mut buf = vec![0u8; MAX_FRAME];
    loop {
        match socket.recv_from(&mut buf) {
            Ok((n, _from)) => match decode(&buf[..n]) {
                Ok(Some(frame)) => {
                    let mut q = shared.queue.lock().unwrap();
                    let dropped = q.push(frame);
                    let mut s = shared.stats.lock().unwrap();
                    s.frames_ok += 1;
                    if dropped {
                        s.frames_dropped_full += 1;
                    }
                }
                Ok(None) => {
                    shared.stats.lock().unwrap().sync_packets += 1;
                }
                Err(_e) => {
                    shared.stats.lock().unwrap().frames_bad += 1;
                }
            },
            Err(e) => {
                // Timeout is expected; everything else, we log silently.
                if e.kind() != std::io::ErrorKind::WouldBlock
                    && e.kind() != std::io::ErrorKind::TimedOut
                {
                    // We don't have a logger crate here; printing once a
                    // while is the cheapest "you have a problem" signal.
                    eprintln!("ifi-transport: recv error: {}", e);
                }
            }
        }
    }
}

/// Decode helper re-exported for convenience.
pub fn decode_frame(buf: &[u8]) -> Result<Option<CsiFrame>, DecodeError> {
    decode(buf)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ifi_core::{encode, CsiFrame};
    use std::net::UdpSocket;

    fn dummy_frame(seq: u32) -> CsiFrame {
        CsiFrame {
            node_id: 1,
            n_antennas: 1,
            n_subcarriers: 4,
            freq_mhz: 2437,
            sequence: seq,
            rssi: -50,
            noise_floor: -95,
            ppdu_type: 0,
            flags: 0,
            iq: vec![1, 2, 3, 4, 5, 6, 7, 8],
        }
    }

    #[test]
    fn ring_buf_drops_oldest_when_full() {
        let mut r = RingBuf::new(2);
        assert!(!r.push(dummy_frame(1)));
        assert!(!r.push(dummy_frame(2)));
        assert!(r.push(dummy_frame(3))); // dropped seq=1
        let d = r.drain();
        assert_eq!(d.len(), 2);
        assert_eq!(d[0].sequence, 2);
        assert_eq!(d[1].sequence, 3);
    }

    #[test]
    fn end_to_end_loopback() {
        // Bind the receiver on an ephemeral port, then send three frames at it.
        let recv = Receiver::bind("127.0.0.1:0".parse().unwrap(), 64).unwrap();
        // We can't read the bound port back through the public API today,
        // so instead bind a known port and try-again on conflict.
        // For the test, use a fresh receiver on a chosen high port.
        drop(recv);

        let port = 56873; // arbitrary
        let recv = Receiver::bind(format!("127.0.0.1:{}", port).parse().unwrap(), 64).unwrap();
        let sender = UdpSocket::bind("127.0.0.1:0").unwrap();

        let mut buf = Vec::new();
        for seq in 1..=3 {
            encode(&dummy_frame(seq), &mut buf);
            sender
                .send_to(&buf, format!("127.0.0.1:{}", port))
                .unwrap();
        }

        // Give the rx thread a moment.
        std::thread::sleep(std::time::Duration::from_millis(150));
        let frames = recv.drain();
        assert_eq!(frames.len(), 3);
        assert_eq!(frames[0].sequence, 1);
        assert_eq!(frames[2].sequence, 3);

        let s = recv.stats();
        assert_eq!(s.frames_ok, 3);
        assert_eq!(s.frames_bad, 0);
    }
}
