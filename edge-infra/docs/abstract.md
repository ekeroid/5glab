## A Noob Installing a 5G Network in Just a Few Hours with Claude

### Abstract

What happens when you hand someone with zero 3GPP experience an Ericsson radio base station, a Kubernetes cluster, and an AI coding assistant? Apparently, a working 5G network — in a few hours rather than the weeks it might otherwise take.

We report on the experience of deploying a fully functional 5G Standalone network by pair-programming with Claude, an AI agent that could read logs, run kubectl commands, and — crucially — explain what "S-NSSAI SD ffffff" means without making us feel stupid. The hardware: an Ericsson BB6651 G3 on band n78 (3.5 GHz, 80 MHz). The core: Open5GS v2.7.2, deployed on Kubernetes via Helm. The vibe: chaotic but productive.

The journey was a greatest-hits compilation of telecom foot-guns. We discovered that the Helm chart hardcodes a bogus PLMN (999/70) in the NRF, causing the SCP to treat our own AMF as a foreign roamer. We learned the hard way that "Operator Key" on a SIM vendor's datasheet actually means OPc, not OP — a distinction that costs exactly one hour of staring at authentication failure logs. We found that provisioning a subscriber with security keys but no UE-AMBR field produces the uniquely unhelpful error "No AccessAndMobilitySubscriptionData." And we accidentally bricked a cell by changing the TDD pattern to one the hardware doesn't support (it got better).

Once UEs were happily attached, we built a custom benchmarking tool and dissected where time actually goes in a 5G round-trip. The answer: almost entirely in the air. The core network adds 0.23 ms; the radio adds 10 ms. We then went knob-twiddling on the gNB via MOSHELL, testing DRX, prescheduling, and TDD pattern changes. Prescheduling won (P99 dropped from 16 ms to 12 ms). DRX did nothing because the UE was already wide awake from our continuous benchmark traffic — an obvious result in hindsight.

The broader takeaway: an AI assistant that can reason across protocol layers, parse vendor-specific log formats, and keep track of fifteen interacting configuration files turns a daunting multi-domain problem into a manageable conversation. The 3GPP specs are still 10,000 pages long, but you no longer need to have read them all before plugging things in.
