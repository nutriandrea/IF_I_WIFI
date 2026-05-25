# Experiments

Anything that isn't proven goes here. The rule is:

1. If you have an idea you want to try (a new DSP step, an on-device
   classifier, a way to fuse multiple ESP32-S3 nodes), build it under a
   subdirectory here.
2. The host workspace at `host/` is reserved for what we believe and
   ship. Don't introduce dependencies from `host/crates/*` onto
   anything in this folder.
3. When an experiment turns into something we actually believe in,
   write a short note in `docs/decisions/` explaining what changed and
   what evidence supports the move out of `experiments/`.

There is no template here on purpose. Pick whatever language and build
system makes sense for the experiment.
