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

func TestTreeSnapshotParsing(t *testing.T) {
	start := treeStart.FindStringSubmatch("TREE count=2 complete=1")
	if start == nil || start[1] != "2" {
		t.Fatalf("tree header not parsed: %#v", start)
	}
	node := treeNode.FindStringSubmatch("0 aa:bb:cc:dd:ee:ff parent=11:22:33:44:55:66 parent_known=1 direct_child=1")
	if node == nil || node[1] != "aa:bb:cc:dd:ee:ff" {
		t.Fatalf("tree node not parsed: %#v", node)
	}
}

func TestAttackResponseIncludesStatus(t *testing.T) {
	response := attackResponse{Action: "status", Status: "NOT_ENOUGH_POINTS_TO_CAPTURE"}
	if response.Status != "NOT_ENOUGH_POINTS_TO_CAPTURE" {
		t.Fatalf("unexpected status: %q", response.Status)
	}
}
