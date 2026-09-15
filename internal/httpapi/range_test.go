package httpapi

import (
	"strings"
	"testing"
)

func TestMeasurementRangeAPI(t *testing.T) {
	h, _ := setup(t)
	body := withLocation(`"metadata":{"minimum":-0.1,"maximum":0.2}`)
	status(t, request(h, "POST", Path, body, testKey), 201)
	read := request(h, "GET", Path+"?id="+firstID, "", testKey)
	status(t, read, 200)
	if !strings.Contains(read.Body.String(), `"minimum":-0.1`) || !strings.Contains(read.Body.String(), `"maximum":0.2`) {
		t.Fatal("range not preserved")
	}
	status(t, request(h, "POST", Path, body, testKey), 200)
}

func TestInvalidMeasurementRangeBatchIsAtomic(t *testing.T) {
	for _, metadata := range []string{`{"minimum":1,"maximum":2}`, `{"minimum":-1}`, `{"minimum":null,"maximum":1}`, `{"minimum":-1,"maximum":"1"}`} {
		t.Run(metadata, func(t *testing.T) {
			h, _ := setup(t)
			bad := strings.ReplaceAll(withLocation(`"metadata":`+metadata), firstID, "12345678-1234-4234-8234-123456789002")
			status(t, request(h, "POST", Path, "["+firstBody+","+bad+"]", testKey), 422)
			status(t, request(h, "GET", Path+"?id="+firstID, "", testKey), 404)
		})
	}
}
