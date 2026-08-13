package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"

	"go.bug.st/serial"
)

var uuidRequest = regexp.MustCompile(`^UUID_REQUEST\s+session=\d+\s+node=([0-9a-fA-F:]{17})\s+uuid=([0-9a-fA-F]{32})\s+attack_points=(\d+)`)
var rgb = regexp.MustCompile(`^[0-9a-f]{6}$`)

type attackRequest struct {
	NodeID       string `json:"node_id"`
	UUID         string `json:"uuid"`
	AttackPoints string `json:"attack_points"`
}

type attackResponse struct {
	Action string `json:"action"`
	Color  string `json:"color"`
	Error  string `json:"error"`
}

type worker struct {
	server string
	secret string
	client *http.Client
	port   serial.Port
	write  sync.Mutex
}

func (w *worker) command(node, command string) {
	w.write.Lock()
	defer w.write.Unlock()
	if _, err := w.port.Write([]byte(command + "\n")); err != nil {
		log.Printf("write %s: %v", node, err)
	}
}

func (w *worker) handle(line string) {
	match := uuidRequest.FindStringSubmatch(strings.TrimSpace(line))
	if match == nil {
		return
	}
	node := strings.ToLower(match[1])
	body, err := json.Marshal(attackRequest{NodeID: node, UUID: strings.ToLower(match[2]), AttackPoints: match[3]})
	if err != nil {
		log.Printf("marshal event: %v", err)
		return
	}
	req, err := http.NewRequest(http.MethodPost, strings.TrimRight(w.server, "/")+"/api/v1/territory-control/device/attacks", bytes.NewReader(body))
	if err != nil {
		log.Printf("build request: %v", err)
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Territory-Secret", w.secret)
	response, err := w.client.Do(req)
	if err != nil {
		// Do not retry an ambiguous request; retrying could spend AP twice.
		log.Printf("attack request for %s: %v", node, err)
		w.command(node, "reject "+node)
		return
	}
	defer response.Body.Close()
	result := attackResponse{}
	if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
		log.Printf("decode attack response for %s: %v", node, err)
		w.command(node, "reject "+node)
		return
	}
	if response.StatusCode >= 200 && response.StatusCode < 300 && result.Action == "color" && rgb.MatchString(strings.ToLower(result.Color)) {
		w.command(node, fmt.Sprintf("color %s #%s", node, strings.ToLower(result.Color)))
		log.Printf("%s: %s -> #%s", node, result.Action, result.Color)
		return
	}
	log.Printf("%s rejected: %s", node, result.Error)
	w.command(node, "reject "+node)
}

func main() {
	server := flag.String("server", "", "CTFd base URL")
	secret := flag.String("secret", "", "TERRITORY_DEVICE_SECRET")
	device := flag.String("port", "", "serial device, e.g. /dev/ttyUSB0")
	baud := flag.Int("baud", 115200, "serial baud rate")
	timeout := flag.Duration("timeout", 10*time.Second, "CTFd HTTP request timeout")
	flag.Parse()
	if *server == "" || *secret == "" || *device == "" {
		flag.Usage()
		return
	}
	port, err := serial.Open(*device, &serial.Mode{BaudRate: *baud})
	if err != nil {
		log.Fatal(err)
	}
	defer port.Close()
	w := &worker{server: *server, secret: *secret, client: &http.Client{Timeout: *timeout}, port: port}
	log.Printf("connected to %s; forwarding UUID_REQUEST events to %s", *device, *server)
	scanner := bufio.NewScanner(port)
	scanner.Buffer(make([]byte, 256), 4096)
	for scanner.Scan() {
		w.handle(scanner.Text())
	}
	log.Fatal(scanner.Err())
}
