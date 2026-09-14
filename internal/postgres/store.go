// Package postgres provides transactional, idempotent storage backed by TimescaleDB.
package postgres

import (
	"context"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"strconv"
	"strings"
	"time"

	"github.com/AndreasWillibaldWeber/DanuBaaS/internal/sensor"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed migrations/*.sql
var migrations embed.FS

type Store struct{ pool *pgxpool.Pool }

func Open(ctx context.Context, dsn string) (*Store, error) {
	config, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, errors.New("invalid database configuration")
	}
	config.MaxConns = 8
	config.MinConns = 1
	config.ConnConfig.ConnectTimeout = 5 * time.Second
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		return nil, err
	}
	if err = pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	return &Store{pool}, nil
}
func (s *Store) Close()                         { s.pool.Close() }
func (s *Store) Ping(ctx context.Context) error { return s.pool.Ping(ctx) }

// Migrate is an explicit administrative operation, never run by the HTTP process.
func (s *Store) Migrate(ctx context.Context) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background()) //nolint:errcheck
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(7349201)`); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `CREATE TABLE IF NOT EXISTS public.sensor_schema_version (version integer PRIMARY KEY)`); err != nil {
		return err
	}
	files, err := fs.Glob(migrations, "migrations/*.sql")
	if err != nil {
		return err
	}
	for _, file := range files {
		base := strings.TrimPrefix(file, "migrations/")
		prefix, _, ok := strings.Cut(base, "_")
		version, err := strconv.Atoi(prefix)
		if !ok || err != nil {
			return fmt.Errorf("invalid migration filename %q", file)
		}
		var applied bool
		if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM public.sensor_schema_version WHERE version=$1)`, version).Scan(&applied); err != nil {
			return err
		}
		if applied {
			continue
		}
		sql, err := migrations.ReadFile(file)
		if err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, string(sql)); err != nil {
			return fmt.Errorf("migration %s: %w", base, err)
		}
		if _, err = tx.Exec(ctx, `INSERT INTO public.sensor_schema_version VALUES($1)`, version); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

func (s *Store) Put(ctx context.Context, values []sensor.Value) ([]sensor.Record, int, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return nil, 0, err
	}
	defer tx.Rollback(context.Background()) //nolint:errcheck
	// Serialize this small demo's writers before allocating sequence numbers. This
	// makes sequence order match commit order, preventing pagination from skipping
	// an earlier sequence committed after a later page was read.
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(7349202)`); err != nil {
		return nil, 0, err
	}
	out := make([]sensor.Record, 0, len(values))
	created := 0
	for _, v := range values {
		payload, err := json.Marshal(v)
		if err != nil {
			return nil, 0, err
		}
		record := sensor.Record{Value: v}
		err = tx.QueryRow(ctx, `INSERT INTO sensor.events(id,observed_at,payload) VALUES($1,$2,$3)
   ON CONFLICT(id) DO NOTHING RETURNING sequence,received_at`, v.ID, v.Timestamp, payload).Scan(&record.Sequence, &record.ReceivedAt)
		if errors.Is(err, pgx.ErrNoRows) {
			var same bool
			err = tx.QueryRow(ctx, `SELECT sequence,received_at,payload=$2::jsonb FROM sensor.events WHERE id=$1`, v.ID, payload).Scan(&record.Sequence, &record.ReceivedAt, &same)
			if err != nil {
				return nil, 0, err
			}
			if !same {
				return nil, 0, sensor.ErrConflict
			}
		} else if err != nil {
			return nil, 0, err
		} else {
			meta, err := json.Marshal(v.Metadata)
			if err != nil {
				return nil, 0, err
			}
			if v.Metadata == nil {
				meta = []byte("{}")
			}
			var longitude, latitude *float64
			if v.LonLat != nil {
				longitude, latitude = &v.LonLat[0], &v.LonLat[1]
			}
			_, err = tx.Exec(ctx, `INSERT INTO sensor.measurements(observed_at,id,sensor_id,gateway_id,sensor_type,value,unit,metadata,longitude,latitude,location_id)
    VALUES($1,$2,$3,NULLIF($4,''),$5,$6,$7,$8,$9,$10,$11)`, v.Timestamp, v.ID, v.SensorID, v.GatewayID, v.SensorType, *v.Value, v.Unit, meta, longitude, latitude, v.LocationID)
			if err != nil {
				return nil, 0, err
			}
			created++
		}
		out = append(out, record)
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, 0, err
	}
	return out, created, nil
}
func (s *Store) Get(ctx context.Context, id string) (sensor.Record, error) {
	var record sensor.Record
	var payload []byte
	err := s.pool.QueryRow(ctx, `SELECT payload,sequence,received_at FROM sensor.events WHERE id=$1`, id).Scan(&payload, &record.Sequence, &record.ReceivedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return record, sensor.ErrNotFound
	}
	if err != nil {
		return record, err
	}
	err = json.Unmarshal(payload, &record.Value)
	return record, err
}
func (s *Store) List(ctx context.Context, after int64, limit int) ([]sensor.Record, error) {
	rows, err := s.pool.Query(ctx, `SELECT payload,sequence,received_at FROM sensor.events WHERE sequence>$1 ORDER BY sequence LIMIT $2`, after, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []sensor.Record{}
	for rows.Next() {
		var record sensor.Record
		var payload []byte
		if err = rows.Scan(&payload, &record.Sequence, &record.ReceivedAt); err != nil {
			return nil, err
		}
		if err = json.Unmarshal(payload, &record.Value); err != nil {
			return nil, err
		}
		out = append(out, record)
	}
	return out, rows.Err()
}
