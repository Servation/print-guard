# Third-party material

## ha-bambulab (MIT License)

`src/print_guard/data/bambu_ca.pem` (Bambu Lab's public CA certificates) and
`src/print_guard/data/hms_en.json.gz` (English HMS and print-error descriptions)
come from [greghesp/ha-bambulab](https://github.com/greghesp/ha-bambulab). The
printer stage names in `printer/state.py` and the protocol details (MQTT topics,
camera login on port 6000) were also learned from that project.

```
MIT License

Copyright (c) 2023 ha-bambulab contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Obico failure-detection model

The spaghetti detector is the ONNX model published by Obico (formerly The
Spaghetti Detective, [TheSpaghettiDetective/obico-server](https://github.com/TheSpaghettiDetective/obico-server),
AGPL-3.0). It is **not** bundled: `print-guard download-model` fetches it from the
URL that Obico's server repository references and checks the SHA-256 published
there. print-guard's inference and smoothing code is its own implementation; the
smoothing follows the approach and default constants Obico documents in its
server code.

## Research sources

* [ClusterM/open-bamboo-networking](https://github.com/ClusterM/open-bamboo-networking):
  SSDP discovery format, the `print.fun` Developer Mode bit, and which commands
  need signatures when Developer Mode is off.
