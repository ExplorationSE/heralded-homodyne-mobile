/* Mock der Teledyne-SP-Devices-Bibliothek ADQAPI (ADQ32, FWDAQ) für Tests ohne Hardware.
 * Funktionsnamen, Signaturen, Strukturen (ADQGen4Record, ADQGen4RecordHeader v2.0, ADQInfoListEntry,
 * ADQDataReadoutStatus), Fehlercodes (ADQ_EOK … ADQ_EINTERNAL) und Parameter-IDs wie im ADQ3 Series User Guide
 * 21-2539 (Rev. 2026.2, Anhang A). Parameter nur über die JSON-Schnittstelle; 64-bit-Ganzzahlen als Zeichenketten
 * (Kap. 15.8.2). Die Standardwerte der Parametersätze sind Annahmen des Mocks. Kein Code von Teledyne.
 *
 * Erzeugt Records in Echtzeit: Kanalquellen ADQ_EVENT_SOURCE_TRIG (Heralds wie im HH500-Mock, +42,7 ns),
 * _PERIODIC (frequency), _SYNC (1-kHz-Marker wie im HH500-Mock, +23 ns, 3,2-ns-Raster), Rearm/Totzeit je Kanal.
 * Umgebungsvariablen: MOCK_ADQ_NODEV=1, MOCK_ADQ_OVERFLOW_AFTER=<Records>, MOCK_HERALD_RATE, MOCK_ADQ_VERSION=-1|-2. */
#include <stdio.h>
#include <string.h>
#include "common.h"

#define ADQ_EOK 0
#define ADQ_EINVAL (-1)
#define ADQ_EAGAIN (-2)
#define ADQ_EOVERFLOW (-3)
#define ADQ_ENOTREADY (-4)
#define ADQ_EINTERRUPTED (-5)
#define ADQ_ANY_CHANNEL (-1)
#define NPID 64
#define NBUF 64
#define TU 50e-12                 /* time_unit = 1/(8·2,5 GHz) */
#define FS 2.5e9

struct ADQGen4RecordHeader {
    uint8_t version_major, version_minor; uint16_t timestamp_synchronization_counter, general_purpose_start,
        general_purpose_stop; uint64_t timestamp; int64_t record_start; uint32_t record_length; uint8_t user_id, misc;
    uint16_t record_status; uint32_t record_number; uint8_t channel, data_format; char serial_number[10];
    uint64_t sampling_period; double time_unit; uint32_t firmware_specific; int32_t reserved;
};
struct ADQGen4Record { struct ADQGen4RecordHeader *header; void *data; uint64_t size; };
struct ADQDataReadoutStatus { uint32_t flags; };
struct ADQInfoListEntry { int HWIFType; int ProductID; unsigned int VendorID, AddressField1, AddressField2;
                          char DevFile[64]; unsigned int DeviceInterfaceOpened, DeviceSetupCompleted; };
_Static_assert(sizeof(struct ADQGen4RecordHeader) == 72, "header v2.0");

typedef struct { struct ADQGen4Record rec; struct ADQGen4RecordHeader hdr; int16_t *data; size_t cap; int used; } slot_t;
typedef struct {
    int setup, running; char *json[NPID]; struct ADQInfoListEntry list[1];
    double t0; uint64_t seed; mock_herald_t her; double next_h; uint64_t mk_k[2]; uint64_t per_n[2];
    double next_t[2], busy_until[2]; uint32_t recno[2]; long n_total; slot_t slot[NBUF];
} cu_t;

static const char *PIDNAME[NPID] = {
    [1] = "ADQ_PARAMETER_ID_DATA_ACQUISITION", [2] = "ADQ_PARAMETER_ID_DATA_TRANSFER",
    [3] = "ADQ_PARAMETER_ID_DATA_READOUT", [4] = "ADQ_PARAMETER_ID_CONSTANT", [8] = "ADQ_PARAMETER_ID_SAMPLE_SKIP",
    [10] = "ADQ_PARAMETER_ID_EVENT_SOURCE_PERIODIC", [11] = "ADQ_PARAMETER_ID_EVENT_SOURCE_TRIG",
    [12] = "ADQ_PARAMETER_ID_EVENT_SOURCE_SYNC", [13] = "ADQ_PARAMETER_ID_ANALOG_FRONTEND",
    [19] = "ADQ_PARAMETER_ID_TOP", [20] = "ADQ_PARAMETER_ID_PORT_TRIG", [21] = "ADQ_PARAMETER_ID_PORT_SYNC",
    [27] = "ADQ_PARAMETER_ID_PORT_GPIOA", [31] = "ADQ_PARAMETER_ID_PULSE_GENERATOR0", [40] = "ADQ_PARAMETER_ID_CLOCK_SYSTEM"};
#define MAGIC "\"magic\": \"12273903644374837623\""
static char *defjson(int pid) {
    char b[4096]; const char *n = PIDNAME[pid];
    switch (pid) {
    case 1: snprintf(b, sizeof b, "{\"id\": \"%s\", \"reserved\": 0, \"channel\": ["
        "{\"horizontal_offset\": \"0\", \"record_length\": \"0\", \"nof_records\": \"0\", \"nof_bits_per_sample\": \"16\", \"compression_gain\": 1.0, \"rearm_length\": \"52\", \"trigger_source\": \"ADQ_EVENT_SOURCE_INVALID\", \"trigger_edge\": \"ADQ_EDGE_RISING\", \"trigger_blocking_source\": \"ADQ_FUNCTION_INVALID\", \"zero_length_records_enabled\": 0, \"dynamic_record_length_enabled\": 0, \"reserved\": 0}, "
        "{\"horizontal_offset\": \"0\", \"record_length\": \"0\", \"nof_records\": \"0\", \"nof_bits_per_sample\": \"16\", \"compression_gain\": 1.0, \"rearm_length\": \"52\", \"trigger_source\": \"ADQ_EVENT_SOURCE_INVALID\", \"trigger_edge\": \"ADQ_EDGE_RISING\", \"trigger_blocking_source\": \"ADQ_FUNCTION_INVALID\", \"zero_length_records_enabled\": 0, \"dynamic_record_length_enabled\": 0, \"reserved\": 0}], " MAGIC "}", n); break;
    case 4: snprintf(b, sizeof b, "{\"id\": \"%s\", \"nof_channels\": 2, \"serial_number\": \"SPD-MOCK1\", \"product_name\": \"ADQ32\", \"product_options\": \"-PCIE -FWDAQ -S2G5 -BW1G0\", \"firmware\": {\"type\": \"ADQ_FIRMWARE_TYPE_FWDAQ\", \"name\": \"MOCK\"}, " MAGIC "}", n); break;
    case 8: snprintf(b, sizeof b, "{\"id\": \"%s\", \"reserved\": 0, \"channel\": [{\"skip_factor\": \"1\", \"synchronization_source\": \"ADQ_FUNCTION_INVALID\", \"reserved\": 0}, {\"skip_factor\": \"1\", \"synchronization_source\": \"ADQ_FUNCTION_INVALID\", \"reserved\": 0}], " MAGIC "}", n); break;
    case 10: snprintf(b, sizeof b, "{\"id\": \"%s\", \"synchronization_source\": \"ADQ_EVENT_SOURCE_INVALID\", \"period\": \"0\", \"high\": \"0\", \"low\": \"0\", \"frequency\": 1000.0, " MAGIC "}", n); break;
    case 11: case 12: snprintf(b, sizeof b, "{\"id\": \"%s\", \"reserved\": 0, \"pin\": [{\"reference_clock_synchronization_enabled\": 0, \"threshold\": 0.5, \"reference_clock_synchronization_edge\": \"ADQ_EDGE_RISING\"}], " MAGIC "}", n); break;
    case 13: snprintf(b, sizeof b, "{\"id\": \"%s\", \"reserved\": 0, \"channel\": [{\"input_range\": 500.0, \"dc_offset\": 0.0}, {\"input_range\": 500.0, \"dc_offset\": 0.0}], " MAGIC "}", n); break;
    case 40: snprintf(b, sizeof b, "{\"id\": \"%s\", \"reserved\": 0, \"clock_generator\": \"ADQ_CLOCK_GENERATOR_INTERNAL_PLL\", \"reference_source\": \"ADQ_REFERENCE_CLOCK_SOURCE_INTERNAL\", \"sampling_frequency\": 2500000000.0, \"reference_frequency\": 10000000.0, \"delay_adjustment\": 0.0, \"low_jitter_mode_enabled\": 1, \"delay_adjustment_enabled\": 0, " MAGIC "}", n); break;
    default: if (!n) return NULL; snprintf(b, sizeof b, "{\"id\": \"%s\", \"reserved\": 0, " MAGIC "}", n);
    }
    return strdup(b);
}
/* n-tes Vorkommen eines Schlüssels: Zahl (auch als Zeichenkette) bzw. Zeichenkette */
static const char *findkey(const char *s, const char *key, int nth) {
    char k[96]; snprintf(k, sizeof k, "\"%s\"", key);
    const char *p = s;
    for (int i = 0; p && i <= nth; i++) { p = strstr(p, k); if (!p) return NULL; if (i < nth) p += strlen(k); }
    p += strlen(k); while (*p == ' ' || *p == ':') p++;
    return p;
}
static double getnum(const char *s, const char *key, int nth, double dflt) {
    const char *p = s ? findkey(s, key, nth) : NULL; if (!p) return dflt; if (*p == '"') p++; return strtod(p, NULL);
}
static int getstr(const char *s, const char *key, int nth, char *out, size_t n) {
    const char *p = s ? findkey(s, key, nth) : NULL; if (!p || *p != '"') return 0; p++;
    size_t i = 0; while (*p && *p != '"' && i + 1 < n) out[i++] = *p++; out[i] = 0; return 1;
}

int ADQAPI_ValidateVersion(int major, int minor) { (void)minor; const char *v = getenv("MOCK_ADQ_VERSION"); if (v) return atoi(v); return major == 15 ? 0 : -1; }
void *CreateADQControlUnit(void) { cu_t *c = calloc(1, sizeof(cu_t)); for (int i = 0; i < NPID; i++) c->json[i] = defjson(i); return c; }
void DeleteADQControlUnit(void *p) {
    cu_t *c = p; if (!c) return;
    for (int i = 0; i < NPID; i++) free(c->json[i]);
    for (int i = 0; i < NBUF; i++) free(c->slot[i].data);
    free(c);
}
int ADQControlUnit_EnableErrorTrace(void *p, unsigned int lvl, const char *dir) {
    (void)p; (void)lvl; char f[1024]; snprintf(f, sizeof f, "%s/spd_adqcontrolunit_trace.log", dir);
    FILE *fh = fopen(f, "a"); if (fh) { fputs("ADQAPI MOCK trace enabled\n", fh); fclose(fh); } return 1;
}
int ADQControlUnit_ListDevices(void *p, struct ADQInfoListEntry **lst, unsigned int *n) {
    cu_t *c = p; if (!c || !lst || !n) return 0;
    *n = getenv("MOCK_ADQ_NODEV") ? 0 : 1;
    memset(c->list, 0, sizeof c->list); c->list[0].ProductID = 32; c->list[0].VendorID = 0x1b37; strcpy(c->list[0].DevFile, "/dev/adq_mock0");
    *lst = c->list; return 1;
}
int ADQControlUnit_SetupDevice(void *p, int idx) {
    cu_t *c = p; if (!c || idx != 0 || getenv("MOCK_ADQ_NODEV")) return 0; c->setup = 1; c->list[0].DeviceSetupCompleted = 1; return 1;
}
#define CU(p, num) cu_t *c = p; if (!c || !c->setup || (num) != 1) return ADQ_EINVAL;
int ADQ_GetParametersString(void *p, int num, int id, char *const s, size_t len, int fmt) {
    CU(p, num) (void)fmt;
    if (id < 0 || id >= NPID || !c->json[id] || !s) return ADQ_EINVAL;
    size_t n = strlen(c->json[id]) + 1; if (n > len) return ADQ_EINVAL;
    memcpy(s, c->json[id], n); return (int)n;
}
int ADQ_SetParametersString(void *p, int num, const char *const s, size_t len) {
    CU(p, num) (void)len;
    if (c->running || !s) return ADQ_EINVAL;
    char idn[96]; if (!getstr(s, "id", 0, idn, sizeof idn)) return ADQ_EINVAL;
    for (int i = 0; i < NPID; i++) if (PIDNAME[i] && strcmp(PIDNAME[i], idn) == 0) {
        if (i == 4) return ADQ_EINVAL;                     /* Konstanten sind nur lesbar */
        if (i == 13) {                                      /* Mock: zulässige Eingangsbereiche */
            for (int k = 0; k < 2; k++) {
                double r = getnum(s, "input_range", k, 500.0);
                if (!(r == 100.0 || r == 250.0 || r == 500.0 || r == 1000.0 || r == 2500.0)) return ADQ_EINVAL;
                double o = getnum(s, "dc_offset", k, 0.0); if (o < -250.0 || o > 250.0) return ADQ_EINVAL;
            }
        }
        free(c->json[i]); c->json[i] = strdup(s); return (int)strlen(s);
    }
    return ADQ_EINVAL;
}
static int ch_source(cu_t *c, int ch) {     /* 0 aus, 1 TRIG, 2 PERIODIC, 3 SYNC */
    char src[96]; if (!getstr(c->json[1], "trigger_source", ch, src, sizeof src)) return 0;
    if (getnum(c->json[1], "record_length", ch, 0) <= 0) return 0;
    if (!strcmp(src, "ADQ_EVENT_SOURCE_TRIG")) return 1;
    if (!strcmp(src, "ADQ_EVENT_SOURCE_PERIODIC")) return 2;
    if (!strcmp(src, "ADQ_EVENT_SOURCE_SYNC")) return 3;
    return 0;
}
static double next_event(cu_t *c, int ch) {
    int s = ch_source(c, ch);
    if (s == 1) { double t = c->next_h + 42.7e-9; return t; }
    if (s == 2) { double f = getnum(c->json[10], "frequency", 0, 1000.0); return (c->per_n[ch] + 1) / f; }
    if (s == 3) { while (mock_marker_omitted(c->mk_k[ch])) c->mk_k[ch]++; return mock_marker_time(c->mk_k[ch]) + 23e-9; }
    return 1e300;
}
static void advance(cu_t *c, int ch) {
    int s = ch_source(c, ch);
    if (s == 1) { /* Herald gilt für alle TRIG-Kanäle: weiter erst, wenn kein anderer TRIG-Kanal darauf wartet */
        int other = 1 - ch;
        if (ch_source(c, other) == 1 && c->next_t[other] == c->next_t[ch]) { c->next_t[ch] = 1e301; return; }
        c->next_h = mock_herald_next(&c->her);
        for (int k = 0; k < 2; k++) if (ch_source(c, k) == 1) c->next_t[k] = next_event(c, k);
        return;
    }
    if (s == 2) c->per_n[ch]++;
    if (s == 3) c->mk_k[ch]++;
    c->next_t[ch] = next_event(c, ch);
}
int ADQ_StartDataAcquisition(void *p, int num) {
    CU(p, num) if (c->running) return ADQ_ENOTREADY;
    if (!ch_source(c, 0) && !ch_source(c, 1)) return ADQ_EINVAL;   /* kein aktiver Kanal */
    c->running = 1; c->t0 = mock_now_s(); c->seed = 0x9E3779B97F4A7C15ull; mock_herald_init(&c->her);
    c->next_h = mock_herald_next(&c->her); c->n_total = 0;
    for (int k = 0; k < 2; k++) { c->mk_k[k] = c->per_n[k] = 0; c->recno[k] = 0; c->busy_until[k] = -1; c->next_t[k] = next_event(c, k); }
    for (int i = 0; i < NBUF; i++) c->slot[i].used = 0;
    return ADQ_EOK;
}
int ADQ_StopDataAcquisition(void *p, int num) { CU(p, num) if (!c->running) return ADQ_ENOTREADY; c->running = 0; return ADQ_EOK; }
int64_t ADQ_WaitForRecordBuffer(void *p, int num, int *channel, void **buffer, int timeout, struct ADQDataReadoutStatus *st) {
    CU(p, num) if (!channel || !buffer || timeout < -1) return ADQ_EINVAL;
    if (st) st->flags = 0;
    if (!c->running) return ADQ_ENOTREADY;
    long ovf = (long)env_d("MOCK_ADQ_OVERFLOW_AFTER", -1);
    if (ovf >= 0 && c->n_total >= ovf) return ADQ_EOVERFLOW;
    double deadline = mock_now_s() + (timeout < 0 ? 1e9 : timeout * 1e-3);
    for (;;) {
        int ch = -1; double t = 1e299;
        for (int k = 0; k < 2; k++) if ((*channel == ADQ_ANY_CHANNEL || *channel == k) && ch_source(c, k) && c->next_t[k] < t) { t = c->next_t[k]; ch = k; }
        if (ch < 0) { if (mock_now_s() >= deadline) return ADQ_EAGAIN; mock_sleep_s(1e-3); continue; }
        double L = getnum(c->json[1], "record_length", ch, 0), ho = getnum(c->json[1], "horizontal_offset", ch, 0);
        double skip = getnum(c->json[8], "skip_factor", ch, 1); if (skip < 1) skip = 1;
        double sp = 8.0 * skip;                                       /* sampling_period in time_unit */
        double ready = c->t0 + t + (L + ho) * sp * TU;                 /* letztes Sample aufgenommen */
        double now = mock_now_s();
        if (ready > now) {
            if (now >= deadline) return ADQ_EAGAIN;
            mock_sleep_s((ready < deadline ? ready : deadline) - now);
            continue;
        }
        if (t < c->busy_until[ch]) { advance(c, ch); continue; }      /* Rearm: Trigger verworfen */
        int sl = -1; for (int i = 0; i < NBUF; i++) if (!c->slot[i].used) { sl = i; break; }
        if (sl < 0) { if (st) st->flags = 1; return ADQ_EAGAIN; }     /* keine freien Puffer: „starving“ */
        slot_t *S = &c->slot[sl];
        size_t need = (size_t)L * 2; if (S->cap < need) { S->data = realloc(S->data, need); S->cap = need; }
        int src = ch_source(c, ch);
        double q = src == 3 ? 64.0 : (src == 2 ? 8.0 : 1.0);           /* Zeitauflösung SYNC 3,2 ns, periodisch 1 Sample */
        uint64_t ts = (uint64_t)(llround(t / TU / q) * q);
        int64_t first = (int64_t)((ts / (uint64_t)sp) * (uint64_t)sp) + (int64_t)(ho * sp);
        memset(&S->hdr, 0, sizeof S->hdr);
        S->hdr.version_major = 2; S->hdr.timestamp = ts; S->hdr.record_start = first - (int64_t)ts;
        S->hdr.record_length = (uint32_t)L; S->hdr.record_number = c->recno[ch]++; S->hdr.channel = (uint8_t)ch;
        S->hdr.data_format = 0; strcpy(S->hdr.serial_number, "SPD-MOCK1"); S->hdr.sampling_period = (uint64_t)sp;
        S->hdr.time_unit = TU;
        for (size_t i = 0; i < (size_t)L; i++) {
            double v = ch == 0 ? 3.0 + 80.0 * mock_gauss(&c->seed) : 1700.0 + 3.0 * mock_gauss(&c->seed);
            S->data[i] = (int16_t)(v > 2047 ? 2047 : (v < -2048 ? -2048 : v));
        }
        S->rec.header = &S->hdr; S->rec.data = S->data; S->rec.size = S->cap; S->used = 1;
        c->busy_until[ch] = t + (L + ho) * sp * TU + 20e-9;
        advance(c, ch);
        *channel = ch; *buffer = &S->rec; c->n_total++;
        return (int64_t)need;
    }
}
int ADQ_ReturnRecordBuffer(void *p, int num, int ch, void *buffer) {
    CU(p, num) (void)ch;
    for (int i = 0; i < NBUF; i++) if (&c->slot[i].rec == buffer) { if (!c->slot[i].used) return ADQ_EINVAL; c->slot[i].used = 0; return ADQ_EOK; }
    return ADQ_EINVAL;
}
