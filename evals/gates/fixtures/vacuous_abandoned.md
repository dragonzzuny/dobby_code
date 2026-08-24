# GATES-VACUOUS-ABANDONED fixture

Both gates were real and both turned out impossible here.

- [ ] GPU: the CUDA path is exercised
  CHECK: nvidia-smi
  EXPECT: NVIDIA
- [ ] NET: the published package resolves
  CHECK: pip download dobby-harness
  EXPECT: Saved
ABANDON: GPU no GPU on this host
ABANDON: NET providers.allow_network is false
