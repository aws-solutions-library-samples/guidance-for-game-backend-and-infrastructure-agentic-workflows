// UDP echo game server — the same game logic as the Agones simple-game-server
// (it answers every datagram with "ACK: <msg>"), but with no Agones SDK
// dependency. On GameLift it runs behind the Containers Starter Kit SDK wrapper,
// which handles InitSDK / ProcessReady / game-session activation. That swap —
// Agones SDK out, GameLift Server SDK in — is the code-level core of an
// Agones-to-GameLift migration.
package main

import (
	"fmt"
	"log"
	"net"
	"os"
	"strings"
)

func main() {
	port := os.Getenv("GAME_PORT")
	if port == "" {
		port = "7654"
	}
	conn, err := net.ListenPacket("udp", ":"+port)
	if err != nil {
		log.Fatalf("listen udp :%s: %v", port, err)
	}
	defer conn.Close()
	log.Printf("echo game server listening on udp :%s", port)

	buf := make([]byte, 1024)
	for {
		n, addr, err := conn.ReadFrom(buf)
		if err != nil {
			log.Printf("read: %v", err)
			continue
		}
		msg := strings.TrimSpace(string(buf[:n]))
		if _, err := conn.WriteTo([]byte(fmt.Sprintf("ACK: %s\n", msg)), addr); err != nil {
			log.Printf("write: %v", err)
		}
	}
}
