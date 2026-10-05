# Vendored Chart.js

This directory contains the Chart.js library and the date-fns adapter used by the DCA-Bot dashboard.
The assets are served locally so the dashboard does not depend on an external CDN.

## Files

| File | Version | Source |
|------|---------|--------|
| `chart.umd.min.js` | 4.4.1 | https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js |
| `chartjs-adapter-date-fns.bundle.min.js` | 3.0.0 | https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js |

## SHA-256 checksums

```
d2af8974e95271638772e9e9524db5b9a6f58d6ec2d5d781400447b4a31c681e  chart.umd.min.js
ea7ab30d26c38dcf1f2d26bb43e73a94537b58f1906f55e1a546dd09321b5615  chartjs-adapter-date-fns.bundle.min.js
```

## Verification

The checksums above are derived from the official jsdelivr/npm artifacts. To verify local files:

```bash
python scripts/verify_vendor_checksums.py
```

This script reads `vendor-manifest.json`, computes SHA-256 hashes of every listed file, and fails if any file is missing, modified, or undocumented.

## Update method

1. Choose the new version from https://www.chartjs.org/ and https://github.com/chartjs/chartjs-adapter-date-fns.
2. Download the new files into this directory from the official npm/jjsdelivr source.
3. Recompute SHA-256 checksums:
   ```bash
   sha256sum web/static/vendor/chart.js/*.js
   ```
4. Update `vendor-manifest.json`, the table, and the checksum block above.
5. Update `web/templates/base.html` only if the filenames change.
6. Run `python scripts/verify_vendor_checksums.py` and the test suite to confirm integrity.

Last verified: 2026-07-26
