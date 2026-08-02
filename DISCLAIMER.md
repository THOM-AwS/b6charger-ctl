# Disclaimer

Every effort has been made to make this project as safe as possible:
frame encoding independently hand-traced against the reference
protocol implementation, safety bounds enforced in code rather than
just documented, a live cell-count cross-check before any charge
starts, dry-run modes throughout, and a documented hardware test
record in [`DRY_RUN.md`](DRY_RUN.md).

None of that makes this risk-free.

**Use of this code is entirely at your own risk.** This software sends
write commands to hardware that regulates LiPo/LiHV battery charging
voltage and current. A wrong configuration, a bug in this code, a
hardware quirk on your specific charger, or a battery pack outside
what's been tested could overcharge, overheat, or otherwise damage a
battery. **Lithium battery fires are real, fast, and can burn a house
down.** You have been warned.

**Read the code before you trust it with a real battery.** This is
open source specifically so you can verify what it actually sends
before it reaches your hardware, not because it's been proven bug-free
- it hasn't, and can't be. `protocol.py` is the part that matters most:
review the frame-building logic yourself, cross-check it against
[libb6](https://github.com/maciek134/libb6) or your charger's own
documentation, and use `--dry-run` to see the exact bytes before
anything is sent for real.

Do not run this unattended. Do not trust it with a battery or a
charger you're not prepared to lose. Do not assume "it worked on this
charger" means it will behave identically on yours - see
[`CONTRIBUTING.md`](CONTRIBUTING.md) for what to do if your hardware
disagrees with what this project expects.

This software is provided under the GPL-3.0-or-later license, which
includes its own explicit disclaimer of warranty (see [`LICENSE`](LICENSE),
Sections 15-16). This file exists in addition to that, in plain
language, because the actual physical stakes here are higher than most
software carries.
