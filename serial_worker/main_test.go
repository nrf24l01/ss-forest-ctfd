package main

import "testing"

func TestUUIDRequestParsing(t *testing.T) {
	match := uuidRequest.FindStringSubmatch("UUID_REQUEST session=7 node=AA:bb:cc:dd:ee:ff uuid=550e8400e29b41d4a716446655440000 attack_points=42")
	if match == nil {
		t.Fatal("valid UUID_REQUEST was not parsed")
	}
	if match[1] != "AA:bb:cc:dd:ee:ff" || match[2] != "550e8400e29b41d4a716446655440000" || match[3] != "42" {
		t.Fatalf("unexpected fields: %#v", match)
	}
}

func TestUUIDRequestRejectsProtocolNoise(t *testing.T) {
	if uuidRequest.MatchString("I (12) root: UUID_REQUEST session=1") {
		t.Fatal("firmware log noise must not be treated as a protocol event")
	}
}
