# Vendored d3 modules

Only the scale and time modules, not the whole of d3: the charts on
/analytics are hand-drawn SVG, and these supply the part that was going
wrong -- choosing ticks and time intervals that stay readable whether the
data spans two hours or two months.

Vendored rather than loaded from a CDN because the server is self-hosted
and a LAN's internet connection is not something the dashboard should
depend on. Each file's own header carries its version and copyright.

Load order matters: every module attaches to the global `d3`, and each
needs the ones listed above it.

| file | version | sha256 |
|---|---|---|
| `d3-array.min.js` | v3.2.4 | `80aa70d0cd17dabddf6d056494ea17926a45a69da8b7850220aace331bad671d` |
| `d3-color.min.js` | v3.1.0 | `a12639010163230b8c130fbeb92a3a49bb5f6989a566d3664d759522db458489` |
| `d3-interpolate.min.js` | v3.0.1 | `bfc321e4c3f3b3aadc88cfe15ccb5e443abfeadef8b75c65b41c33a4d78a98ae` |
| `d3-format.min.js` | v3.1.0 | `7d053f71a135100128802b6010ab58d1351ca412e2d7846c2f8c6fe155a66370` |
| `d3-time.min.js` | v3.1.0 | `0e67c6eed5832f4ac5bece0da0ea595860c4527d21c271a17013b7efe2665c00` |
| `d3-time-format.min.js` | v4.1.0 | `ce7c943affe8cbb049de1c4ee29ce98bd4cb4b393bd48cb0c7b4f8078c4752f5` |
| `d3-scale.min.js` | v4.0.2 | `e76a84839ffba3b94fef22ea1e39da8398fa0e039c7e6a0a93b7d938dbd50632` |

Source: `https://cdn.jsdelivr.net/npm/<module>@<version>/dist/<module>.min.js`,
downloaded 2026-09-25. License: ISC (Copyright Mike Bostock), permitting
use and redistribution with the copyright notice retained -- which the
header comment at the top of each file does.
