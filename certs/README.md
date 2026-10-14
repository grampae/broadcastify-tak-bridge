# TAK client certificates

Put the following PEM files in this directory:

- `client.pem`: The TAK client certificate.
- `client.key`: The matching private key.
- `ca.pem`: The certificate authority that issued the TAK Server certificate.

The Git ignore rules exclude all other files in this directory.

**Warning:** Do not commit private keys, PKCS#12 files, certificate passwords, or production certificates.
