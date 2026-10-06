# GameLift SDK wrapper (vendored)

`server.go`, `go.mod`, and `go.sum` are copied unmodified from the Amazon
GameLift Servers Containers Starter Kit:
https://github.com/aws/amazon-gamelift-toolkit/tree/main/containers-starter-kit/SdkGoWrapper

Licensed under the Apache License 2.0 (see the upstream repository's LICENSE).

The wrapper initializes the GameLift Go Server SDK (5.2.0, downloaded at image
build time), calls `ProcessReady` for the game port, activates game sessions,
and calls `ProcessEnding` on shutdown, so an existing server binary can run on a
GameLift container fleet without code changes.
