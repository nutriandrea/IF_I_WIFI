//! `ifiwifi-capture` — receive ADR-018 CSI frames over UDP and print stats.
//!
//! Designed to be the smallest possible "is my sensor working?" tool.
//! No async runtime, no JSON output by default, no telemetry. Stdout
//! prints one summary line per second. Optional `--csv` writes a row per
//! frame to a file (header excluded).
//!
//! Examples:
//!
//! ```text
//! ifiwifi-capture --bind 0.0.0.0:5555
//! ifiwifi-capture --bind 0.0.0.0:5555 --csv frames.csv --with-iq
//! ifiwifi-capture --bind 0.0.0.0:5555 --hampel
//! ```

use std::collections::HashMap;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use clap::{Parser, Subcommand};

use ifi_core::CsiFrame;
use ifi_dsp::{
    frame_mean_amplitude, CoherenceConfig, CoherenceDecision, CoherenceGate, HampelConfig,
    StreamingHampel,
};
use ifi_transport::Receiver;

#[derive(Parser, Debug)]
#[command(
    name = "ifiwifi-capture",
    version,
    about = "Receive ADR-018 CSI frames over UDP and print stats."
)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand, Debug)]
enum Cmd {
    /// Listen on a UDP address and print one summary line per second.
    Capture {
        /// Local bind address, e.g. `0.0.0.0:5555`.
        #[arg(long, default_value = "0.0.0.0:5555")]
        bind: String,

        /// Optional CSV output file (one row per frame).
        #[arg(long)]
        csv: Option<PathBuf>,

        /// If set with --csv, also write the I/Q payload as a hex blob.
        #[arg(long, requires = "csv")]
        with_iq: bool,

        /// Apply a streaming Hampel filter to per-frame mean amplitude and
        /// report the outlier count in the per-second summary.
        #[arg(long)]
        hampel: bool,

        /// Enable the coherence gate and include its decision in the summary.
        #[arg(long)]
        coherence: bool,

        /// Stop after this many seconds (0 = run forever).
        #[arg(long, default_value_t = 0)]
        seconds: u64,
    },
}

fn main() {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Capture {
            bind,
            csv,
            with_iq,
            hampel,
            coherence,
            seconds,
        } => {
            if let Err(e) = run_capture(&bind, csv, with_iq, hampel, coherence, seconds) {
                eprintln!("error: {}", e);
                std::process::exit(1);
            }
        }
    }
}

fn run_capture(
    bind: &str,
    csv: Option<PathBuf>,
    with_iq: bool,
    use_hampel: bool,
    use_coherence: bool,
    seconds: u64,
) -> Result<(), Box<dyn std::error::Error>> {
    let addr = bind.parse()?;
    let rx = Receiver::bind(addr, 4096)?;
    println!("ifiwifi-capture listening on {}", bind);

    let mut csv_writer = match csv {
        Some(path) => {
            let f = File::create(&path)?;
            let mut w = BufWriter::new(f);
            if with_iq {
                writeln!(w, "t_us,node_id,seq,rssi,noise,n_sub,freq_mhz,iq_hex")?;
            } else {
                writeln!(w, "t_us,node_id,seq,rssi,noise,n_sub,freq_mhz")?;
            }
            Some(w)
        }
        None => None,
    };

    let mut hampel = use_hampel.then(|| StreamingHampel::new(HampelConfig::default()));
    let mut gate = use_coherence.then(|| CoherenceGate::new(CoherenceConfig::default()));

    // Per-second aggregation
    let started = Instant::now();
    let mut last_tick = Instant::now();
    let mut sec_index: u64 = 0;
    let mut sec_count = 0u64;
    let mut sec_rssi_sum: i64 = 0;
    let mut sec_outliers = 0u64;
    let mut last_seen_seq: HashMap<u8, u32> = HashMap::new();
    let mut last_gate_decision = CoherenceDecision::Recalibrate;

    loop {
        std::thread::sleep(Duration::from_millis(100));

        for frame in rx.drain() {
            sec_count += 1;
            sec_rssi_sum += i64::from(frame.rssi);
            last_seen_seq.insert(frame.node_id, frame.sequence);

            // Optional DSP
            if let Some(h) = hampel.as_mut() {
                let amp = frame_mean_amplitude(&frame);
                let step = h.push(amp);
                if step.is_outlier {
                    sec_outliers += 1;
                }
            }
            if let Some(g) = gate.as_mut() {
                let amp = frame_mean_amplitude(&frame);
                last_gate_decision = g.step(amp);
            }

            // Optional CSV
            if let Some(w) = csv_writer.as_mut() {
                write_csv_row(w, &frame, with_iq)?;
            }
        }

        if last_tick.elapsed() >= Duration::from_secs(1) {
            sec_index += 1;
            let stats = rx.stats();
            let mean_rssi = if sec_count > 0 {
                (sec_rssi_sum / sec_count as i64) as i32
            } else {
                0
            };
            let nodes = last_seen_seq.len();
            let last_seq_str = last_seen_seq
                .iter()
                .map(|(n, s)| format!("n{}:{}", n, s))
                .collect::<Vec<_>>()
                .join(",");

            let mut line = format!(
                "[+{}s] sps={:>3} mean_rssi={:>4} drops={} bad={} sync={} nodes={} last_seq=[{}]",
                sec_index,
                sec_count,
                mean_rssi,
                stats.frames_dropped_full,
                stats.frames_bad,
                stats.sync_packets,
                nodes,
                last_seq_str
            );
            if use_hampel {
                line.push_str(&format!(" hampel_outliers={}", sec_outliers));
            }
            if use_coherence {
                line.push_str(&format!(" gate={:?}", last_gate_decision));
            }
            println!("{}", line);

            // Reset per-second accumulators (lifetime counters live in rx.stats()).
            sec_count = 0;
            sec_rssi_sum = 0;
            sec_outliers = 0;
            last_tick = Instant::now();
        }

        if seconds > 0 && started.elapsed().as_secs() >= seconds {
            break;
        }
    }

    if let Some(mut w) = csv_writer {
        w.flush()?;
    }
    Ok(())
}

fn write_csv_row(
    w: &mut BufWriter<File>,
    f: &CsiFrame,
    with_iq: bool,
) -> std::io::Result<()> {
    let t_us = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_micros())
        .unwrap_or(0);
    if with_iq {
        let hex = hex_encode(&f.iq);
        writeln!(
            w,
            "{},{},{},{},{},{},{},{}",
            t_us, f.node_id, f.sequence, f.rssi, f.noise_floor, f.n_subcarriers, f.freq_mhz, hex
        )?;
    } else {
        writeln!(
            w,
            "{},{},{},{},{},{},{}",
            t_us, f.node_id, f.sequence, f.rssi, f.noise_floor, f.n_subcarriers, f.freq_mhz
        )?;
    }
    Ok(())
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        use std::fmt::Write;
        let _ = write!(s, "{:02x}", b);
    }
    s
}
