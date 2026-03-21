# Getting Credentials (No Android Required)

This component needs two values:
- `hub_id`
- `network_passphrase_hex`

You can retrieve both from in-lite API login response.

## Fast path (recommended): interactive wizard

Set up a virtual environment first:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

Then run the wizard:

```bash
python3 tools/inlite_config_wizard.py
```

What it does:
- asks for email
- requests login code (`/v2/users/authorize`)
- asks for the one-time code
- logs in (`/user/login`)
- shows all returned gardens and lets you select one if multiple are returned
- prints selected `hub_id` + `passphrase_hex`
- generates a full ESPHome YAML file
- asks whether to verify connectivity immediately
  - if `yes`: runs discovery, turns lines `1,2,3` ON, then turns `1,2,3` OFF using the harness
- writes raw diagnostics files under the current directory for each run:
  - `./.inlite_wizard/run_<timestamp>_<pid>/`
  - `authorize_request.json`
  - `authorize_response.raw.txt`
  - `authorize_response.parsed.json`
  - `login_request.json`
  - `login_response.raw.txt`
  - `login_response.parsed.json`
  - `verify/01_scan.log`
  - `verify/02_line_<n>_on.log`
  - `verify/03_line_<n>_off.log`

---

## 1) Request login code

```bash
curl -sS https://api.inlite.coffeeit.nl/v2/users/authorize \
  -H 'Content-Type: application/json' \
  --data '{"email":"YOUR_EMAIL","language":"en"}'
```

You will receive a one-time code by email.

## 2) Login and save response

```bash
curl -sS https://api.inlite.coffeeit.nl/user/login \
  -H 'Content-Type: application/json' \
  --data '{"email":"YOUR_EMAIL","code":"CODE_FROM_EMAIL"}' > /tmp/inlite_login.json
```

## 3) Extract hub IDs and passphrase hex

```bash
python3 - <<'PY'
import json
from pathlib import Path

obj = json.loads(Path('/tmp/inlite_login.json').read_text())
for g in obj.get('gardens', []):
    pwd = g.get('password', '')
    for t in g.get('transformers', []):
        gid = t.get('deviceId')
        if isinstance(gid, int):
            tname = t.get('name', '')
            print(f"name={g.get('name','')} transformer={tname} hub_id_dec={gid} hub_id_hex=0x{gid:04X} passphrase_hex={pwd.encode('utf-8').hex()}")
PY
```

Pick your garden row.

- Use `hub_id_hex` as `hub_id`.
- Use `passphrase_hex` as `network_passphrase_hex`.

## 4) Optional: discover BLE address

Not required for normal ESP autodiscovery flow, but useful for diagnostics.

```bash
python3 tools/inlite_ble_harness.py scan --seconds 12 --name-filter inlite
```

## 5) Put secrets in ESPHome

Example `secrets.yaml` entries:

```yaml
inlite_smart_hub_mesh_id: 0x163E
inlite_smart_hub_passphrase_hex: c38e78...
```

## Security

- Do not commit your real passphrase or API responses.
- Delete `/tmp/inlite_login.json` after extracting values.
