# GameLift SDK wrapper (vendored)

`server.go`, `go.mod`, and `go.sum` are copied from the Amazon GameLift
Servers Containers Starter Kit:
https://github.com/aws/amazon-gamelift-toolkit/tree/main/containers-starter-kit/SdkGoWrapper

Licensed under the Apache License 2.0: see `LICENSE` and `NOTICE` in this
directory, copied from the upstream repository.

Changes from upstream: `server.go` is unmodified. `go.mod` and `go.sum` raise
`github.com/gorilla/websocket` from v1.5.1 to v1.5.3
([GHSA-w67g-5rqw-f597](https://github.com/advisories/GHSA-w67g-5rqw-f597)) and
are re-tidied against Server SDK 5.2.0, which drops the `golang/mock` and
`golang.org/x/net` requirements that only websocket v1.5.1 needed. Dependabot
cannot raise it here, because the `replace` directive points at the SDK
directory that `../build.sh` downloads at build time.

The wrapper initializes the GameLift Go Server SDK (5.2.0, downloaded at image
build time), calls `ProcessReady` for the game port, activates game sessions,
and calls `ProcessEnding` on shutdown, so an existing server binary can run on a
GameLift container fleet without code changes.
