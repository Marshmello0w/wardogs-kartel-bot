# WARDOGS Artillery Calculator

Vendored build of [apollyon-sys/wardogs-calculator](https://github.com/apollyon-sys/wardogs-calculator), commit `d96c15ffc2c65dc31b4cceff8a9b12a7724467a6` (2026-09-23). The upstream MIT license is retained as `LICENSE`.

Generated with `npm run build`, then `node web/tools/vendor_artillery.mjs <upstream-dist>`. The vendoring step applies the Das Kartell dark/orange theme, removes upstream analytics, disables origin-bound live lobbies and feedback, and points map tile/terrain URLs to this portal's restricted same-origin asset route. The portal fetches those files from the upstream asset release on first use and caches copies in `web/data/artillery_assets/`; that cache is not in Git and may grow as visitors explore new map areas.

WARDOGS game artwork, map tiles, logos, trademarks, and other third-party materials are not included in the upstream MIT license. The operator is responsible for ensuring permission to host the cached map assets. The page visibly credits [wardogs-artillery.com](https://wardogs-artillery.com/) as the creator.
