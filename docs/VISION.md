# If-I-Wi-Fy

### *Ambient intelligence through the radio waves that already surround you.*

If-I-Wi-Fy turns the Wi-Fi signals already passing through every room into a privacy-preserving sense of presence, motion, and context. Built on the Arduino UNO Q — a dual-brain board that fuses a Linux AI processor with a real-time microcontroller in a single 50€-100€ device — If-I-Wi-fy demonstrates that the next generation of ambient intelligence does not require new cameras, new wearables, or new infrastructure. It only requires us to *listen* to what the air is already telling us. The technology stack we are showing today detects human presence in a room using nothing but radio perturbations and on-device machine learning. The platform we are pointing at addresses healthcare, security, energy, workplace optimization, retail, and industrial safety — all from the same architecture.

---

## 1. The world is over-sensored and under-aware

In the last decade, the world has been filled with sensors: cameras in every corner, microphones in every speaker, wearables on every wrist, motion detectors, smart locks, smart bulbs, smart thermostats, smart doorbells.

Despite this technological saturation, the dominant sensing paradigms have hit a structural wall:

- **Cameras** are powerful but invasive. They generate legal liability under GDPR and the AI Act, they create attack surfaces, they're refused by users in the spaces they would matter most (bathrooms, bedrooms, elderly homes, changing rooms, classrooms etc ...).
- **Microphones** raise the same concerns and are largely confined to voice-assistant contexts.
- **Wearables** require user compliance. The people who would benefit most — the elderly, dementia patients, children — are the least likely to wear them reliably.
- **PIR motion sensors** see only motion, not presence. A person reading on a couch becomes invisible to them within seconds.
- **Single-purpose IoT sensors** (CO₂, door, occupancy beacons) work but multiply hardware, installation cost, and maintenance costs. *Every new question requires a new device*.

The result is a paradox: a world saturated with data acquisition but starved of contextual understanding. We measure the room's temperature without knowing if anyone is in it. We record video without ever wanting to look at it. We deploy occupancy sensors that miss the static occupant. We hand out wearables that end up in drawers.

If-I-Wi-fy begins from the observation that the most omnipresent signal in human civilization — the Wi-Fi that fills every building — is already a sensor. We just haven't been listening.

---

## 2. The insight: Wi-Fi is already a sense

Every Wi-Fi access point continuously radiates electromagnetic waves into its surroundings. That energy reflects, refracts, and is absorbed by every surface it encounters. When a person enters a room, their body becomes a new scatterer in the radio environment. The signal that reaches any nearby receiver is subtly but measurably altered: amplitude, phase, multipath structure, and timing all shift.

This is not speculation. It is documented physics, exploited for years in academic research (notably by the Politecnico di Milano, MIT CSAIL, Tsinghua, and dozens of others) to demonstrate Wi-Fi-based:

- Presence detection
- Motion classification
- Person counting
- Pose estimation
- Gait recognition
- Breathing rate monitoring
- Heartbeat detection through walls
- Fall detection

Until recently, these demonstrations required specialized research hardware or custom-coded equipment. What has changed — and what If-I-Wi-fy is built on — is that single-board computers with general-purpose Linux now have enough processing power to run the machine learning pipelines that turn raw radio measurements into actionable understanding. The bottleneck was platform accessibility, and is now gone.

> **The thesis of If-I-Wi-fy:** The walls of every building you have ever entered are humming with information you can't hear. We are building the platform that translates it.

---

## 3. The platform: Arduino UNO Q and the dual-brain advantage TODO: aggiorna in base a quello che facciamo davvero

If-I-Wi-fy runs on the Arduino UNO Q — and the choice of platform is not incidental. The UNO Q is one of the first consumer-priced boards that natively addresses the architectural problem of edge AI: real-time deterministic control and high-level machine learning have historically required two different devices.

The UNO Q collapses both into one:

| Component | Role |
|---|---|
| Qualcomm Dragonwing™ QRB2210 (quad-core ARM Cortex-A53 @ 2 GHz) | Runs Debian Linux. Hosts the If-I-Wi-fy Python pipeline: signal acquisition, feature extraction, ML inference, dashboard, cloud sync. |
| STM32U585 (ARM Cortex-M33, Zephyr OS) | Runs Arduino sketches. Handles deterministic real-time I/O: sensor reads, actuators, relays, LED feedback. |
| Dual-band Wi-Fi 5 (2.4 / 5 GHz) + Bluetooth 5.1 | The sensing substrate itself. |
| 2–4 GB LPDDR4X RAM, 16–32 GB eMMC | Enough to train, run, and log ML pipelines on-device. |
| UNO form factor, USB-C, ~€60 retail | Deployable at consumer scale. |

This architecture matters because it makes a previously enterprise-grade capability available for the cost of a smart bulb. A single board, the size of an original Arduino, can simultaneously listen to the radio environment, run a neural classifier on the result, and switch a physical relay with millisecond latency.

We are not waiting for the future of edge AI hardware. It is on the desk, and it costs less than a dinner for two.

---

## 4. How If-I-Wi-fy works // TODO: controlla anche in base a cosa facciamo realmente

If-I-Wi-fy is a three-layer system. Each layer is intentionally simple, and each is generalizable to applications beyond the proof of concept.

**Layer 1 — Acquisition.** The Linux side of the UNO Q continuously samples the radio environment through standard 802.11 mechanisms. Signals from all reachable access points are red at a fixed cadence, building a time series of the room's radio fingerprint. No special drivers: the data is what any laptop's Wi-Fi card already sees — we simply look at it as a signal, not as connection metadata.

**Layer 2 — Feature engineering.** The raw radio time series is transformed into a vector of descriptive features over a sliding window: statistical moments (mean, variance, range, kurtosis), spectral content (energy in frequency bands corresponding to human breathing, slow motion, fast motion), and multi-access-point covariance structure. This is the same kind of transformation used in speech recognition, ECG analysis, and seismic monitoring — applied here to the Wi-Fi spectrum.

**Layer 3 — Inference.** A lightweight machine learning model classifies each window into states (empty, static occupancy, motion) and feeds an anomaly detector that compares the present moment to the room's learned baseline. The model trains in seconds on a few minutes of labeled data per room. The entire pipeline runs on the Linux side of a single Arduino-class board, with no cloud dependency.

When the inference layer fires a decision, it flows back to the STM32 microcontroller via the on-board UART bridge, which translates it into real-world action: a light turns on, an alert is sent, a dashboard updates. The whole loop, from radio perturbation to physical response, closes in under five seconds.

What is on the board today is a demonstrator. What it is *demonstrating* is an architecture that scales horizontally across every vertical described below.

---

## 5. The machine learning layer: anomaly detection as a universal primitive

Underneath all of the verticals above, a single technical pattern emerges over and over: *learn what is normal for this space, then notice when reality departs from it*.

This is the anomaly detection paradigm, and it generalizes across every application domain If-I-Wi-fy touches. The bathroom occupied for forty-five minutes is anomalous. The office occupied at 3 a.m. on a Tuesday is anomalous. The elderly tenant who has not moved between rooms today is anomalous. The retail aisle with thirty percent more foot traffic than usual is anomalous. 

A single anomaly detection layer, trained on the radio fingerprint of any specific space, becomes the foundation for an unbounded set of applications. The model does not need to know in advance what kind of anomaly will occur — it only needs to know what normal looks like. This is what makes If-I-Wi-fy a *platform* rather than a product.

The implication for developers is significant: the same sensing layer, the same UNO Q board, the same Python pipeline, can be specialized to eldercare in one deployment, energy management in the next, and security in a third. The marginal cost of adding a new application is the cost of relabeling data, not the cost of rebuilding the stack.

---

## 6. Where this matters

The same architecture, re-trained on different labels, addresses a portfolio of problems that today require radically different solutions.

### Healthcare and assisted living

The demographic shift in Europe is producing a generation of elderly people living alone for longer than any previous generation in history. Falls are their leading cause of hospitalization and their leading cause of mortality after hospitalization. Existing solutions all fail at the human level: cameras are refused, wearables are forgotten, panic buttons are left on the nightstand.

A passive Wi-Fi sensing system fails none of these. It detects:

- Prolonged immobility (a fall, a stroke, an episode)
- Disruption of normal nocturnal patterns (potential sleep apnea, restlessness)
- Day-over-day deterioration in mobility (early dementia, frailty progression)
- Bathroom occupancy beyond reasonable duration (medical emergency)
- Wandering at unusual hours

Nothing is recorded. Nothing requires the user's cooperation. The system simply notices that the radio environment has changed in a way that matters, and notifies the caregiver who needs to know.

This is the vertical where the privacy-preserving nature of If-I-Wi-fy transitions from a feature to a moral necessity. The same patient who would refuse a camera in their bedroom is the one whose welfare is most at stake.

### Privacy-first security

Home and office security have until now been built on cameras, a paradigm that quietly trades external safety for internal vulnerability. Every camera installed becomes a potential leak point, a jurisdiction issue, an attack surface, a hostile witness during a divorce.

Wi-Fi sensing offers an alternative: detect intrusion, occupancy anomalies, and unauthorized presence without ever generating a recoverable record of what people look like or what they did. It works in the dark, through walls, and across floors. It cannot be turned into evidence in a custody dispute or leaked onto a forum.

Use cases: residential perimeter monitoring, after-hours office anomaly detection, hotel turnover verification, eldercare facility checks, childcare and school safety in spaces where cameras are legally restricted.

### Smart workplaces

The way we use buildings has changed. Whether it’s an office, a university campus, or a healthcare facility, we are often paying for space that sits empty or is poorly utilized. Traditional tracking (badge readers, calendars, or manual sensors) is either too expensive to install or only tells half the story.

If-I-Wi-fy provides a continuous, room-level view of how spaces are actually used, without requiring new hardware. This data allows managers to optimize their real estate, detect "ghost" bookings, and make smarter decisions about their space.

Most importantly, the system is private by design: it monitors occupancy without ever identifying individuals, making it fully GDPR-compliant and friction-free for users.

### Energy and ecology

Most of a building's energy is spent on climate control and lighting—and much of it is wasted on empty rooms. Current systems rely on fixed schedules or motion sensors that can’t "see" someone sitting still. This gap between where the building thinks people are and where they actually are is the biggest source of wasted energy in real estate today.

Wi-Fi sensing solves this by providing real-time, room-by-room occupancy data. The results are immediate: heating and lighting respond to actual use rather than just reservations. Industry estimates show this can cut energy costs significantly across Europe.


### Retail, hospitality, and public spaces

Foot traffic analytics without cameras. Queue length without facial recognition. Empty dressing rooms, available bathrooms, free meeting rooms, occupied library seats — all detected by the same primitive. 

In every one of these settings, the key product feature is not the data itself but its *legitimacy*. Decisions made on Wi-Fi sensing data do not require consent banners, retention policies, or GDPR impact assessments at the same level. The architecture is privacy-preserving as a property of the physics, not as a promise from the manufacturer.

### Industrial and safety

Worker presence in hazardous zones. Lone-worker monitoring in remote installations. Discrimination between humans and equipment near heavy machinery. Evacuation verification confirming that a building is empty after an alarm, without sending a person to check. All addressable by the same sensing primitive.

---


## 7. Why it's the right time for If-I-Wi-fy

Several independent trends are converging to make If-I-Wi-fy viable today, where it would have been impractical even three years ago:

- **Edge AI has matured.** Scikit-learn, ONNX Runtime, and TensorFlow Lite all run comfortably on ARM Linux at consumer prices. Models that required workstations in 2020 now run on 50€ - 100€ boards.
- **Wi-Fi 5 and 6 are universal.** Every building, every coffee shop, every home is now a continuous source of the signal If-I-Wi-fy needs. There is no deployment cost for the substrate — it is already there.
- **Privacy regulation is tightening.** GDPR, the EU AI Act, and parallel legislation in the US and UK make camera-based sensing legally heavier every year. Solutions that are privacy-preserving by construction will inherit growing structural advantages.
- **Demographic pressure on healthcare is undeniable.** Europe's population is aging faster than its caregiving infrastructure can grow. Non-invasive, compliance-free monitoring is no longer a luxury but a requirement.
- **Energy mandates are becoming binding.** ESG reporting, building energy performance directives, and net-zero commitments are forcing the question of occupancy-aware building management out of the optional column.
- **Wearable fatigue is real.** The first generation of "every person carries a sensor" hardware has produced fatigue, drawer-fill rates, and abandonment numbers that the industry no longer denies.

We are at a turning point: affordable hardware, powerful AI, and new regulations have finally aligned to move Wi-Fi sensing out of the laboratory and into the market. If-I-Wi-Fi is ready to lead this shift.

---

## 8. Roadmap // TODO: cambia in base a quanto rusciamo a fare

**Today (proof of concept).** Single-board Wi-Fi sensing demonstrator on the Arduino UNO Q. Presence and motion detection via RSSI feature extraction. On-device Random Forest classification. Closed-loop actuation through the STM32 microcontroller. Optional Arduino Cloud sync for remote dashboards.

**Next 3–6 months.** Migration to Channel State Information (CSI) extraction on supported chipsets, unlocking finer-grained sensing (breathing rate, gait, posture). Multi-modal fusion with environmental sensors (temperature, humidity, air quality, light) for richer context. Multi-room mesh deployment with coordinated learning across nodes.

**Next 6–18 months.** Vertical-specific reference applications — eldercare monitor, building energy optimizer, privacy-preserving security node — packaged as deployable templates. Open dataset for the developer community. Integration patterns for Home Assistant, OpenHAB, and major BMS protocols.

**Longer term.** A platform layer for ambient intelligence — the equivalent of what Home Assistant is to home automation, but for sensing rather than control. A common substrate on which vertical applications are built, with a thriving developer ecosystem.

---

## 9. What If-I-Wi-fy is, in one more sentence

It is the moment when we stop adding sensors to the world and start using the signals already in it.

The cameras can stay off. The wearables can stay in the drawer. The walls will tell us what we need to know — quietly, continuously, and without ever recording a face or a word. We have spent a century teaching radio waves to carry our messages to each other. We are about to teach them to carry our awareness of each other.

Built on €60 of open hardware. Running entirely on the edge. Privacy-preserving by construction. Generalizable to every domain where presence, motion, and context matter.

The infrastructure is already in the room.

We are just the first ones to listen.


**Tagline candidates:**
- *Ambient intelligence through the radio waves that already surround you.*
- *The room already knows. We just translate.*
- *Sense before you watch.*
- *u*
