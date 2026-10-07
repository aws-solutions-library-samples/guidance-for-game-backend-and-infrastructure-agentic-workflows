#!/bin/bash
# Entry point (adapted from the GameLift Containers Starter Kit wrapper.sh).
# Starts the GameLift SDK wrapper (InitSDK + ProcessReady on PORT), then the
# game server; signals the wrapper on exit so it calls ProcessEnding().
PORT="${GAME_PORT:-7654}"

echo "Starting GameLift SDK wrapper on port ${PORT}"
./gameliftwrapper "${PORT}" &
WRAPPER_PID=$!

echo "Starting game server"
GAME_PORT="${PORT}" ./echoserver

echo "Game server terminated; signalling wrapper so it calls ProcessEnding()"
kill -SIGINT "${WRAPPER_PID}"
sleep 0.3
