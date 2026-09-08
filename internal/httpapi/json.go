package httpapi

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/url"
	"unicode/utf8"
)

var parseRawQuery = url.ParseQuery

// uniqueKeys rejects ambiguous input before Go's decoder can silently keep the last key.
func uniqueKeys(body []byte) error {
	if !utf8.Valid(body) {
		return errors.New("body must be valid UTF-8")
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	if err := walkJSON(dec, 0); err != nil {
		return err
	}
	if _, err := dec.Token(); err != io.EOF {
		return errors.New("expected exactly one JSON document")
	}
	return nil
}
func walkJSON(dec *json.Decoder, depth int) error {
	if depth > 32 {
		return errors.New("JSON nesting exceeds 32 levels")
	}
	token, err := dec.Token()
	if err != nil {
		return errors.New("malformed JSON")
	}
	delim, ok := token.(json.Delim)
	if !ok {
		return nil
	}
	switch delim {
	case '{':
		seen := map[string]bool{}
		for dec.More() {
			key, err := dec.Token()
			if err != nil {
				return errors.New("malformed JSON")
			}
			s, ok := key.(string)
			if !ok || seen[s] {
				return errors.New("duplicate JSON object key")
			}
			seen[s] = true
			if err := walkJSON(dec, depth+1); err != nil {
				return err
			}
		}
	case '[':
		for dec.More() {
			if err := walkJSON(dec, depth+1); err != nil {
				return err
			}
		}
	default:
		return errors.New("malformed JSON")
	}
	if _, err := dec.Token(); err != nil {
		return errors.New("malformed JSON")
	}
	return nil
}
