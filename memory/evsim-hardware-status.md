---
name: evsim-hardware-status
description: EVSim hardware status as of 2026-09-07 - custom PCB not yet delivered, protoboard prototype works and already reads the CP PWM
metadata:
  type: project
---

As of **2026-09-07**: the custom PCB has **not arrived yet**. Alexandre has a working **protoboard**
prototype, and **CP PWM reading already works** on it.

**Why:** the PWM-measurement half of [[evsim-tb-overview]] is already de-risked on real hardware, so the
open work is the duty->current->power conversion, the REST control path, and the MQTT plumbing - not the
signal acquisition.

**How to apply:** don't treat CP PWM capture as unproven. Do re-check it once the real PCB arrives, since
the analog front end will change.
