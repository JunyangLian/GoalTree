# Synthetic local TLS receiver

`receiver-key.pem` is an intentionally public test-only private key, paired with
`receiver-cert.pem` for `localhost`. It is not a production credential or CA.
Never use it outside the loopback test receiver or install it into a system trust
store. Tests scope trust with SSL_CERT_FILE and keep hostname/TLS verification on.

The fixtures and canary strings exercise disclosure blocking; they contain no
real user identity, production secret or account credential.
