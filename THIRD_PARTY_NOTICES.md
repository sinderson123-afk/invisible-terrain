# Third-party dependencies and notices

Invisible Terrain's original source and documentation are licensed under [MIT](LICENSE). That license does not replace or relicense any external dependency. This source repository does not vendor Python, native SDR/audio libraries, USB drivers, device firmware, Wallpaper Engine, or third-party wallpaper assets.

The following are upstream projects used or optionally loaded at runtime. Links identify upstream license information, not a guarantee that every downstream binary has the same contents or additional notices.

| Component | Use | Upstream license / source |
| --- | --- | --- |
| Python | Interpreter | [Python license and included components](https://docs.python.org/3/license.html) |
| NumPy | IQ/FFT processing | [BSD-3-Clause and included-component notices](https://numpy.org/doc/stable/license.html) |
| SciPy | Optional FM DSP | [BSD-3-Clause; see source license and bundled-component notices](https://github.com/scipy/scipy/blob/main/LICENSE.txt) |
| python-sounddevice | Optional audio output binding | [MIT](https://github.com/spatialaudio/python-sounddevice/blob/master/LICENSE) |
| PortAudio | Optional native audio output | [PortAudio's MIT-style license](https://portaudio.com/license.html) |
| Osmocom rtl-sdr | Optional native RTL receiver library, installed separately | [GPL-2.0-or-later declaration](https://github.com/osmocom/rtl-sdr/blob/master/include/rtl-sdr.h), [license text](https://github.com/osmocom/rtl-sdr/blob/master/COPYING) |
| RTL-SDR Blog rtl-sdr fork | Optional device-specific RTL library, installed separately | [GPL-2.0-or-later declaration](https://github.com/rtlsdrblog/rtl-sdr-blog/blob/master/include/rtl-sdr.h) |
| libusb | Dependency of some externally installed SDR libraries | [LGPL-2.1-or-later; upstream COPYING](https://github.com/libusb/libusb/blob/master/COPYING) |
| SoapySDR | Optional experimental RX interface | [Boost Software License 1.0](https://github.com/pothosware/SoapySDR/blob/master/LICENSE_1_0.txt) |
| Soapy device modules / vendor libraries | Optional hardware support, installed separately | Separate licenses apply; inspect the particular module and distribution |
| Wallpaper Engine | Optional external wallpaper host | Proprietary, separately licensed; not included |

Package installers may resolve further dependencies or wheels containing additional libraries. Consult the licenses and notices shipped with the exact installed packages, including their transitive/native dependencies. This table is an inventory aid, not a complete binary-distribution bill of materials.

No GPL-covered RTL library binary or source is copied into this repository. If you distribute a combined application, native libraries, drivers, wheels, or an executable bundle, review the relevant licenses and source/notice obligations first. A separate download or dynamic loading does not itself settle the licensing requirements of a combined distribution. This repository's MIT license is not permission to omit upstream obligations.

The terrain is drawn procedurally using the browser Canvas API and system font fallbacks. It does not include code, textures, models or media from another Workshop wallpaper. Product and project names identify interoperability targets; they do not imply endorsement.
