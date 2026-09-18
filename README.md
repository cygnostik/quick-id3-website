# Quick·ID3 website and downloads

Website: **https://quickid3.com**

This public repository contains the Quick·ID3 website and tested application
binaries in [Releases](https://github.com/cygnostik/quick-id3-website/releases).
The desktop and terminal application source is maintained together in the private
`cygnostik/quick-id3` repository; it is not included here.

## Applications

- **Desktop:** graphical MP3 metadata editor for album-release preparation.
  Current desktop version: **0.1.0-beta.2**, macOS Apple Silicon and Windows x64.
- **Terminal:** independent, artwork-led terminal edition. Mac, Linux and Windows
  downloads will be listed only after their native release checks pass.

Download requirements and signing information are stated in each release. These
are proprietary beta applications, free to evaluate, not open-source app releases.
By downloading, you agree to the evaluation license included with the application.
Third-party software retains its own accompanying terms.

## Repository boundaries

Website source belongs here. Application source, build environments, credentials,
live feature-request databases, local evidence and internal project inputs do not.
GitHub publication does not automatically deploy the live site or replace its data.
The website retains its existing one-click downloads and private feature-request
workflow.

Brand assets and application screenshots retain their existing rights. No blanket
open-source license is granted by publication of this website repository. Included
third-party fonts and resources retain the license notices supplied alongside them.

## Source layout and local checks

- `site/` — current frontend, approved screenshots, fonts and original notices.
- `templates/` — server-rendered feature-request form, outside the document root.
- `serve.py`, `feature_server.py`, `request_store.py` — loopback preview and private store.
- `deployment/` — Apache/PHP adapter, fixed-route wrappers and public-file manifest.

With Python 3.9+ and PHP CLI installed, run:

```sh
python3 -B -m unittest test_request_store -v
python3 -B deployment/test_adapter.py
```

These tests use disposable fixtures, not real submissions or release files.
For a local preview, download the original desktop release files and supply them:

```sh
python3 -B feature_server.py --port 8766 \
  --dmg /path/to/QuickID3-0.1.0-beta.2-macOS-arm64.dmg \
  --windows-zip /path/to/QuickID3-0.1.0-beta.2-Windows-x64.zip \
  --db private/preview.sqlite3
```

Open `http://127.0.0.1:8766`. The Python servers are loopback previews, not
production servers. The store changes permissions on its database and parent;
always use a dedicated private directory outside `site/`, never a shared directory.

The production adapter assumes Apache/PHP, HTTPS, `/usr/bin/python3`, the literal
public domain and a sibling private runtime directory. Runtime `adapter.php`,
`request_bridge.py` and `request_store.py` must be siblings, with `templates/`,
`downloads/` and `private/` beneath that private runtime. Only manifest-resolved
frontend files and the five public wrappers belong in the public document root.
`deployment/test_adapter.py` demonstrates assembly in a disposable directory.
No deployment automation, hosting credentials or live databases are included.
