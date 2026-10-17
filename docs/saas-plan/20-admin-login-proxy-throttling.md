# Admin login protection behind the frontend proxy

Production requests `e5f47d90-28b9-4660-8d30-dba5574e167f` and
`2b92f2ec-072f-422a-82c2-4047306c6884` returned backend429. The failed-attempt
counter for the frontend container's IP had reached5, grouping unrelated clients.
Counters below the blocking threshold also accumulated without a time window.

The application now accepts X-Real-IP only when the direct socket peer resolves
to an explicitly configured trusted proxy host. Production trusts its private
Compose `web` service. Docker DNS resolution keeps this trust current when web
is recreated. Invalid/multiple addresses, untrusted peers and DNS failures fall
back to the direct peer. Default trust is empty. Uvicorn proxy-header processing
remains disabled.

The frontend forwards X-Real-IP. This deployment requires Nginx to overwrite it
with its socket client's address; the public web/API ports remain bound to loopback.
Never enable proxy trust behind an edge that lets visitors choose this header.
Nginx retains its own client-IP request limit.

Migration0017 adds last_failure_at. The backend keeps separate account/client-IP
counters, counts5 failed logins within15minutes, blocks for15minutes, resets
expired failure windows, and returns Retry-After while blocked. Successful login
resets the matching counters. Active blocks are not removed by migration.

The form shows a countdown, prevents early resubmission and preserves the typed
password on throttling/transient failures. Invalid credentials still clear the
password, with a support request ID. No password reset, role change, hidden login
or disabling of protection is included.
