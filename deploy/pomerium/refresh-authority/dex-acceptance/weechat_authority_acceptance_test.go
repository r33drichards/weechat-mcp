//go:build weechat_acceptance

package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"acceptance.local/refreshcoord"
	"github.com/dexidp/dex/server/internal"
	"github.com/dexidp/dex/server/tokens"
	"github.com/dexidp/dex/storage/sql"
	"github.com/stretchr/testify/require"
	"golang.org/x/oauth2"
)

// This acceptance harness sends real loopback HTTP requests through Dex's full
// token endpoint. Only the connector and identity fixtures are synthetic.
func TestWeechatAuthorityRealDexHTTP(t *testing.T) {
	for _, useSQLite := range []bool{false, true} {
		t.Run(fmt.Sprintf("sqlite=%v", useSQLite), func(t *testing.T) { testWeechatAuthorityRealDexHTTP(t, useSQLite) })
	}
}

func testWeechatAuthorityRealDexHTTP(t *testing.T, useSQLite bool) {
	for _, ambiguous := range []bool{false, true} {
		t.Run(fmt.Sprintf("ambiguous=%v", ambiguous), func(t *testing.T) {
			server, dex := newTestServer(t, func(c *Config) {
				c.SessionConfig = nil
				c.RefreshTokenPolicy = tokens.NewRefreshStrategy(true, 0, 0, 0, nil)
				if useSQLite {
					db, err := (&sql.SQLite3{File: filepath.Join(t.TempDir(), "dex.db")}).Open(newLogger(t))
					require.NoError(t, err)
					c.Storage = db
				}
			})
			defer server.Close()
			server.Client().Timeout = 5 * time.Second
			mockRefreshTokenTestStorage(t, dex.storage, false)
			raw, err := internal.Marshal(&internal.RefreshToken{RefreshId: "test", Token: "bar"})
			require.NoError(t, err)
			dir := filepath.Join(t.TempDir(), "authority")
			authority, err := refreshcoord.Open(dir)
			require.NoError(t, err)
			defer func() { _ = authority.Close() }()
			initial, err := authority.Register("dex/test", &oauth2.Token{AccessToken: "fixture-access", RefreshToken: raw})
			require.NoError(t, err)
			var calls atomic.Int32
			httpRefresh := func(ctx context.Context, in *oauth2.Token) (*oauth2.Token, error) {
				calls.Add(1)
				values := url.Values{"grant_type": {"refresh_token"}, "refresh_token": {in.RefreshToken}}
				req, err := http.NewRequestWithContext(ctx, "POST", server.URL+"/token", strings.NewReader(values.Encode()))
				if err != nil {
					return nil, err
				}
				req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
				req.SetBasicAuth("test", "barfoo")
				res, err := server.Client().Do(req)
				if err != nil {
					return nil, err
				}
				defer res.Body.Close()
				body, err := io.ReadAll(res.Body)
				if err != nil {
					return nil, err
				}
				if res.StatusCode != http.StatusOK {
					return nil, fmt.Errorf("Dex status %d: %s", res.StatusCode, body)
				}
				var out oauth2.Token
				if err := json.Unmarshal(body, &out); err != nil {
					return nil, err
				}
				if out.RefreshToken == in.RefreshToken {
					return nil, errors.New("Dex did not rotate")
				}
				return &out, nil
			}
			if ambiguous {
				_, err = authority.Refresh(t.Context(), "dex/test", initial, func(ctx context.Context, in *oauth2.Token) (*oauth2.Token, error) {
					// Dex completes rotation over HTTP, then the consumer loses the response.
					_, err := httpRefresh(ctx, in)
					if err != nil {
						return nil, err
					}
					return nil, io.ErrUnexpectedEOF
				})
				require.ErrorIs(t, err, refreshcoord.ErrUnavailable)
				_, err = authority.Refresh(t.Context(), "dex/test", initial, httpRefresh)
				require.ErrorIs(t, err, refreshcoord.ErrUnavailable)
				require.NoError(t, authority.Close())
				authority, err = refreshcoord.Open(dir)
				require.NoError(t, err)
				_, err = authority.Refresh(t.Context(), "dex/test", initial, httpRefresh)
				require.ErrorIs(t, err, refreshcoord.ErrUnavailable)
				require.Equal(t, int32(1), calls.Load(), "ambiguity must not repeat HTTP request")
				t.Log("Dex accepted and rotated once; simulated lost response prevented every subsequent HTTP call, including after authority restart")
				return
			}
			// Simulate alternate manager and browser consumers holding stale copies of
			// the same opaque reference, then concurrently exercise those copies.
			manager, browser := *initial, *initial
			for _, consumer := range []*oauth2.Token{&manager, &browser, &manager, &browser} {
				out, err := authority.Refresh(t.Context(), "dex/test", consumer, httpRefresh)
				require.NoError(t, err)
				require.Equal(t, initial.RefreshToken, out.RefreshToken)
			}
			require.NoError(t, authority.Close())
			authority, err = refreshcoord.Open(dir)
			require.NoError(t, err)
			var wg sync.WaitGroup
			for i := range 12 {
				wg.Add(1)
				go func() {
					defer wg.Done()
					consumer := &manager
					if i%2 == 1 {
						consumer = &browser
					}
					_, err := authority.Refresh(t.Context(), "dex/test", consumer, httpRefresh)
					if err != nil {
						t.Error(err)
					}
				}()
			}
			wg.Wait()
			require.Equal(t, int32(16), calls.Load())
			// Directly replay the original raw bearer to prove Dex's policy is strict.
			_, err = httpRefresh(t.Context(), &oauth2.Token{RefreshToken: raw})
			require.Error(t, err)
			require.Contains(t, err.Error(), "invalid_grant")
			t.Log("16 serialized HTTP refreshes through real Dex succeeded across alternate/concurrent stale consumers and restart; direct stale bearer replay rejected with invalid_grant")
		})
	}
}
