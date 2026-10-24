// compositor-broker-probe — prove the UI-independent compositor→broker→root
// chain. Connects to the running compositor (/tmp/freebsdx.sock) and issues
// AudioOutput.SetActiveSink to the OTHER sink, then checks whether the root
// broker actually moved hw.snd.default_unit. Restores the original sink after.
//
// Build: cc -O2 -o compositor-broker-probe scripts/compositor-broker-probe.c
// Run:   ./compositor-broker-probe        (needs the compositor up)
// Exit 0 = the sink unit changed as requested (full chain verified).

#include <sys/socket.h>
#include <sys/un.h>
#include <sys/sysctl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>

enum { XBUS_MAGIC = 0x58425553u };           // "XBUS"
enum { OP_SET_ACTIVE_SINK = 0x02u };

#pragma pack(push,1)
struct XBusMessage {
    uint32_t magic;
    uint32_t transaction_id;
    char     service_name[32];
    uint32_t opcode;
    uint8_t  payload[256];
};
#pragma pack(pop)

static int cur_unit(void) {
    int v = -1; size_t n = sizeof(v);
    if (sysctlbyname("hw.snd.default_unit", &v, &n, NULL, 0) != 0) return -1;
    return v;
}

// Send AudioOutput.SetActiveSink("pcm<unit>") to the compositor.
static int set_sink(int unit) {
    int fd = socket(AF_UNIX, SOCK_SEQPACKET, 0);
    if (fd < 0) return -1;
    struct sockaddr_un a; memset(&a, 0, sizeof(a));
    a.sun_family = AF_UNIX;
    const char* p = getenv("FBX_SOCK"); strncpy(a.sun_path, (p && p[0]) ? p : "/tmp/freebsdx.sock", sizeof(a.sun_path)-1);
    if (connect(fd, (struct sockaddr*)&a, sizeof(a)) != 0) { close(fd); return -1; }

    struct XBusMessage m; memset(&m, 0, sizeof(m));
    m.magic = XBUS_MAGIC; m.transaction_id = 1;
    strncpy(m.service_name, "com.freebsdx.AudioOutput", sizeof(m.service_name)-1);
    m.opcode = OP_SET_ACTIVE_SINK;
    snprintf((char*)m.payload, 12, "pcm%d", unit);   // AudioSinkSelectRequest.id_[12]
    ssize_t w = send(fd, &m, sizeof(m), MSG_EOR);
    /* drain the ack, best-effort */ struct XBusMessage r; (void)recv(fd, &r, sizeof(r), 0);
    close(fd);
    return (w == (ssize_t)sizeof(m)) ? 0 : -1;
}

int main(void) {
    int start = cur_unit();
    if (start < 0) { printf("  [FAIL] can't read hw.snd.default_unit\n"); return 1; }
    int target = (start == 0) ? 1 : 0;
    printf("== compositor→broker chain probe ==\n");
    printf("  start hw.snd.default_unit = %d; requesting switch to pcm%d via AudioOutput\n", start, target);

    if (set_sink(target) != 0) { printf("  [FAIL] couldn't reach the compositor (is startx up?)\n"); return 1; }
    usleep(400000);
    int after = cur_unit();
    int ok = (after == target);
    printf("  [%s] after SetActiveSink(pcm%d): hw.snd.default_unit = %d\n",
           ok ? "OK" : "FAIL", target, after);

    // Restore the original sink regardless.
    set_sink(start);
    usleep(200000);
    printf("  restored to pcm%d (now %d)\n", start, cur_unit());
    printf("== %s ==\n", ok ? "COMPOSITOR→BROKER→ROOT VERIFIED (UI-independent)"
                             : "chain did NOT move the sink — see above");
    return ok ? 0 : 1;
}
