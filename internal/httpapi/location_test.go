package httpapi

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

func withLocation(fields string) string {
	if fields == "" {
		return firstBody
	}
	return strings.TrimSuffix(firstBody, "}") + "," + fields + "}"
}

func TestOptionalLocationRoundTrip(t *testing.T) {
	for _, fields := range []string{
		"", `"lon_lat":null,"location_id":null`, `"lon_lat":[16.3738,48.2082]`,
		`"location_id":7`, `"lon_lat":[0,0],"location_id":0`,
		`"lon_lat":[-180,-90],"location_id":9007199254740991`,
		`"lon_lat":[180,90],"location_id":null`, `"lon_lat":null,"location_id":7`,
	} {
		t.Run(fields, func(t *testing.T) {
			h, _ := setup(t)
			body := withLocation(fields)
			created := request(h, "POST", Path, body, testKey)
			status(t, created, 201)
			var input, got map[string]json.RawMessage
			if err := json.Unmarshal([]byte(body), &input); err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal(created.Body.Bytes(), &got); err != nil {
				t.Fatal(err)
			}
			for _, key := range []string{"lon_lat", "location_id"} {
				want := string(input[key])
				if want == "null" {
					want = ""
				}
				if string(got[key]) != want {
					t.Fatalf("%s = %s, want %s", key, got[key], want)
				}
			}
			read := request(h, "GET", Path+"?id="+firstID, "", testKey)
			status(t, read, 200)
			if read.Body.String() != created.Body.String() {
				t.Fatal("GET changed location")
			}
			list := request(h, "GET", Path, "", testKey)
			status(t, list, 200)
			if strings.TrimSpace(list.Body.String()) != "["+strings.TrimSpace(created.Body.String())+"]" {
				t.Fatal("list changed location")
			}
			status(t, request(h, "POST", Path, body, testKey), 200)
		})
	}
}

func TestLocationValidationIsAtomic(t *testing.T) {
	for _, tc := range []struct {
		field string
		code  int
	}{
		{`"lon_lat":[]`, 400}, {`"lon_lat":[1]`, 400}, {`"lon_lat":[1,2,3]`, 400},
		{`"lon_lat":[null,1]`, 400}, {`"lon_lat":[1,null]`, 400}, {`"lon_lat":["1",2]`, 400},
		{`"lon_lat":{}`, 400}, {`"lon_lat":false`, 400}, {`"lon_lat":[1e309,0]`, 400},
		{`"lon_lat":[180.01,0]`, 422}, {`"lon_lat":[-180.01,0]`, 422},
		{`"lon_lat":[0,90.01]`, 422}, {`"lon_lat":[0,-90.01]`, 422},
		{`"location_id":-1`, 422}, {`"location_id":9007199254740992`, 422},
		{`"location_id":1.5`, 400}, {`"location_id":"7"`, 400}, {`"location_id":true`, 400},
		{`"location_id":[]`, 400}, {`"location_id":9223372036854775808`, 400},
	} {
		for _, batch := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/batch=%t", tc.field, batch), func(t *testing.T) {
				h, s := setup(t)
				body := withLocation(tc.field)
				if batch {
					body = "[" + firstBody + "," + strings.ReplaceAll(body, firstID, "12345678-1234-4234-8234-123456789002") + "]"
				}
				status(t, request(h, "POST", Path, body, testKey), tc.code)
				if s.puts != 0 {
					t.Fatal("invalid location reached storage")
				}
			})
		}
	}
}

func TestLocationRetryIdentity(t *testing.T) {
	for _, nullFirst := range []bool{false, true} {
		t.Run(fmt.Sprint(nullFirst), func(t *testing.T) {
			h, _ := setup(t)
			a, b := firstBody, withLocation(`"lon_lat":null,"location_id":null`)
			if nullFirst {
				a, b = b, a
			}
			status(t, request(h, "POST", Path, a, testKey), 201)
			status(t, request(h, "POST", Path, b, testKey), 200)
			for _, changed := range []string{`"lon_lat":[0,0]`, `"location_id":0`} {
				status(t, request(h, "POST", Path, withLocation(changed), testKey), 409)
			}
		})
	}
	for _, changed := range []string{`"lon_lat":[1,0],"location_id":7`, `"lon_lat":[0,0],"location_id":8`, `"lon_lat":null,"location_id":7`} {
		h, s := setup(t)
		status(t, request(h, "POST", Path, withLocation(`"lon_lat":[0,0],"location_id":7`), testKey), 201)
		fresh := strings.ReplaceAll(firstBody, firstID, "12345678-1234-4234-8234-123456789002")
		status(t, request(h, "POST", Path, "["+fresh+","+withLocation(changed)+"]", testKey), 409)
		if len(s.records) != 1 {
			t.Fatal("location conflict partially committed batch")
		}
	}
}
