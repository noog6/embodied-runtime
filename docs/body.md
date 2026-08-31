# Semantic body boundary

`BodyBackend` implements robot-level semantic embodiment and is deliberately
distinct from the low-level `HardwareBackend`. The current
`VirtualBodyBackend` implements exactly one capability, `orientation`, in yaw
and pitch degrees. It starts neutral and applies changes instantaneously; there
is no polling, interpolation, motion model, or body kinematics.

`RuntimeState.body`, rather than the backend, is authoritative. A capability
call completes at the backend before the application replaces that immutable
state. Virtual yaw bounds of -180 through 180 degrees and pitch bounds of -90
through 90 degrees are semantic geometry, not servo or other physical limits.
A future physical body can enforce a smaller body model and can use a real
`HardwareBackend` beneath this semantic boundary.
