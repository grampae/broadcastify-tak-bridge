# Runtime data

The feed manager writes its status file and local SQLite databases to this directory. Git ignores those files.

Build the optional address databases with these commands:

```bash
docker compose run --rm manpack-feeds \
  python3 build_address_db.py --service marquette \
  --output /app/data/marquette_addresses.db

docker compose run --rm manpack-feeds \
  python3 build_address_db.py --service ri \
  --output /app/data/ri_addresses.db
```
