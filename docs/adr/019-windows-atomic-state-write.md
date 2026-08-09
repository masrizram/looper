# ADR-019: Windows Atomic State Write Retry Mechanism

## Status
Accepted (Implemented in v2.2.1)

## Context
`looper/state.py` menggunakan `os.replace()` untuk menulis state file secara atomik. Pada Windows, `os.replace()` dapat mengakibatkan `PermissionError: [WinError 5]` ketika:
- Handle file target masih terbuka (oleh proses lain, termasuk thread polling HTTP /status)
- Proses kekurangan hak istimewa SeCreateFilePrivilege
- Antivirus atau shell extension memegang handle

Ini menyebabkan `--dry-run` dan `--run` crash pada Windows tanpa Docker/WSL.

## Evidence
```
$ looper --dry-run --goal "build a CLI todo app"
...
ERROR looper.state: PermissionError: [WinError 5] Access is denied
Traceback (most recent call last):
  File "looper/state.py", line 107, in save
    os.replace(tmp_name, self.state_file)
  ...
  File "looper/cli.py", line 123, in main
    ...
```

## Decision
Gunakan retry mechanism dengan exponential backoff untuk `os.replace()`, dan fallback ke `shutil.copy2` (non-atomic) jika semua retry gagal. Ini memungkinkan Looper berjalan pada host Windows tanpa Docker.

### Implementation Details
- Retry maksimal 5 kali dengan backoff: 20ms, 40ms, 80ms, 160ms
- Jika `PermissionError` persisten, gunakan `shutil.copy2` sebagai fallback
- Jika fallback `copy2` juga gagal, raise exception asli
- Log warning ketika fallback digunakan, karena crash di tengah copy dapat menyebabkan file state corrupt

## Consequences

### Positive
- Windows host tanpa Docker/WSL dapat menjalankan `--dry-run` dan `--run` ( dengan catatan: --doctor masih mengembalikan exit 5 untuk sandbox, tapi state persistence tidak crash)
- Perilaku POSIX tidak berubah (os.replace sudah atomic dan reliable)
- Test coverage tetap 100% — semua test lama diperbarui untuk memverifikasi perilaku baru

### Negative
- Fallback `copy2` tidak atomic — jika proses crash di tengah copy, state file bisa corrupt
- Delay tambahan (maksimal ~300ms) pada Windows saat handle contention
- `RuntimeWarning` dikeluarkan ketika fallback digunakan — ini disengaja untuk memungkinkan observability

## Alternatives Considered
1. **Gunakan SQLite sebagai state backend** — lebih robust, tapi menambah dependensi dan kompleksitas. Ditolak karena filosofi minimalis.
2. **Gunakan portalocker** — third-party library untuk file locking. Ditolak karena ingin minimal dependensi.
3. **Hapus os.replace, pakai copy langsung** — kehilangan atomicity guarantee. Ditolak karena state corruption lebih buruk.

## Related
- Gap #6: Windows State Persistence Atomic Write Failure (HIGH)
- docs/GAP_ANALYSIS.md

## Tags
#state #windows #atomic-write #retry #resilience