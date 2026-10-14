# Optional address database

The service can use a private SQLite database for exact address matches.
Set `locations.address_database` to its container path.

The database must contain this table:

```sql
CREATE TABLE addresses (
    address TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL
);
```

The service performs a case-insensitive exact match on `address`.
Git ignores database files in this directory.
