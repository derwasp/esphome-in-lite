# Getting Credentials (No Android Required)

This component needs two values:
- `hub_id`
- `network_passphrase_hex`

You can retrieve both from in-lite API login response.

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

## 3) Extract garden IDs and passphrase hex

```bash
python3 - <<'PY'
import json
from pathlib import Path

obj = json.loads(Path('/tmp/inlite_login.json').read_text())
for g in obj.get('gardens', []):
    gid = int(g.get('_id', 0))
    pwd = g.get('password', '')
    print(f"name={g.get('name','')} hub_id_dec={gid} hub_id_hex=0x{gid:04X} passphrase_hex={pwd.encode('utf-8').hex()}")
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
