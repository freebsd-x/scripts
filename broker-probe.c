// broker-probe — verify the running root xsessiond's privilege broker
// end-to-end, with ZERO system changes. Connects to /var/run/xsessiond.sock
// (same socket the compositor uses) and checks:
//   1. Hello advertises the Privileged capability  → this is the new daemon
//   2. RunPrivilegedOp(unknown verb)  → PrivilegedResult = -EINVAL (validate+refuse)
//   3. RunPrivilegedOp(audio.default_unit, <current unit>) → 0 (root exec; no change)
//
// Build: cc -O2 -o broker-probe scripts/broker-probe.c
// Run:   ./broker-probe [audio_unit]      (audio_unit defaults to 0)
//        XSESSIOND_SOCK=... ./broker-probe to override the socket path.
//
// Exit 0 = all checks passed.

#include <sys/socket.h>
#include <sys/un.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>

enum { MAGIC = 0x46534431u, VERSION = 1 };
enum { OP_HELLO = 1, OP_RUNPRIV = 8 };
enum { RP_WELCOME = 1, RP_PRIVRESULT = 8 };
enum { CAP_PRIVILEGED = 1 << 4 };

struct ReqHdr { uint32_t magic; uint16_t version; uint16_t opcode;
                uint32_t serial; uint32_t f1; uint32_t f2; };
struct RepHdr { uint32_t magic; uint16_t version; uint16_t reply;
                uint32_t serial; int32_t result; };

static const char* sock_path(void) {
    const char* e = getenv("XSESSIOND_SOCK");
    return (e && e[0]) ? e : "/var/run/xsessiond.sock";
}

// One-shot: connect, send (hdr + f1 + f2), recv RepHdr. Returns reply.result,
// or INT32_MIN on any transport error. *out_reply gets the reply opcode.
static int32_t call(uint16_t op, const char* f1, const char* f2, uint16_t* out_reply) {
    int fd = socket(AF_UNIX, SOCK_SEQPACKET, 0);
    if (fd < 0) return INT32_MIN;
    struct sockaddr_un a; memset(&a, 0, sizeof(a));
    a.sun_family = AF_UNIX;
    strncpy(a.sun_path, sock_path(), sizeof(a.sun_path) - 1);
    if (connect(fd, (struct sockaddr*)&a, sizeof(a)) != 0) { close(fd); return INT32_MIN; }

    uint32_t l1 = f1 ? (uint32_t)strlen(f1) : 0, l2 = f2 ? (uint32_t)strlen(f2) : 0;
    struct ReqHdr h = { MAGIC, VERSION, op, 1, l1, l2 };
    char buf[1024];
    if (sizeof(h) + l1 + l2 > sizeof(buf)) { close(fd); return INT32_MIN; }
    memcpy(buf, &h, sizeof(h));
    if (l1) memcpy(buf + sizeof(h), f1, l1);
    if (l2) memcpy(buf + sizeof(h) + l1, f2, l2);
    if (send(fd, buf, sizeof(h) + l1 + l2, MSG_EOR) < 0) { close(fd); return INT32_MIN; }

    struct RepHdr r; ssize_t n = recv(fd, &r, sizeof(r), 0);
    close(fd);
    if (n != (ssize_t)sizeof(r) || r.magic != MAGIC) return INT32_MIN;
    if (out_reply) *out_reply = r.reply;
    return r.result;
}

int main(int argc, char** argv) {
    const char* unit = (argc > 1) ? argv[1] : "0";
    int pass = 1;

    printf("== xsessiond privilege-broker probe (%s) ==\n", sock_path());

    uint16_t rep = 0;
    int32_t caps = call(OP_HELLO, NULL, NULL, &rep);
    if (caps == INT32_MIN || rep != RP_WELCOME) {
        printf("  [FAIL] Hello — daemon not reachable (is xsessiond running?)\n");
        return 1;
    }
    int has_priv = (caps & CAP_PRIVILEGED) != 0;
    printf("  [%s] Hello caps=0x%x — Privileged capability %s\n",
           has_priv ? "OK" : "FAIL", caps, has_priv ? "present" : "MISSING (old daemon?)");
    pass &= has_priv;

    int32_t bad = call(OP_RUNPRIV, "definitely.not.allowed", "", &rep);
    int bad_ok = (rep == RP_PRIVRESULT && bad == -EINVAL);
    printf("  [%s] RunPrivilegedOp(unknown verb) → %d (want %d, reply=%u)\n",
           bad_ok ? "OK" : "FAIL", bad, -EINVAL, rep);
    pass &= bad_ok;

    int32_t aud = call(OP_RUNPRIV, "audio.default_unit", unit, &rep);
    int aud_ok = (rep == RP_PRIVRESULT && aud == 0);
    printf("  [%s] RunPrivilegedOp(audio.default_unit, %s) → %d (want 0; no-op: current unit)\n",
           aud_ok ? "OK" : "FAIL", unit, aud);
    pass &= aud_ok;

    printf("== %s ==\n", pass ? "BROKER VERIFIED (root exec + validation work)" : "BROKER CHECK FAILED");
    return pass ? 0 : 1;
}
