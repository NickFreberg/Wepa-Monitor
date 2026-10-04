"""Threat intelligence (KEV, Vulnrichment, NVD, MSRC, EPSS, Exploit-DB, Metasploit) and the OWASP checklist."""
from __future__ import annotations

import json

from wepa_monitor import owasp, threatintel, vulns

KEV = {"vulnerabilities": [{"cveID": "CVE-2021-44228", "vulnerabilityName": "Apache Log4j2 RCE", "dateAdded": "2021-12-10",
                            "dueDate": "2021-12-24", "requiredAction": "Apply updates.",
                            "knownRansomwareCampaignUse": "Known"}, {"cveID": "not-a-cve"}]}
EDB = ("id,file,description,date_published,author,type,platform,port,date_added,date_updated,verified,codes,tags\n"
       '50592,exploits/java/remote/50592.py,"Apache Log4j2 2.14.1 - RCE",2021-12-14,x,remote,java,,2021-12-14,'
       "2021-12-14,1,CVE-2021-44228;OSVDB-1,\n"
       "1,exploits/x/1.txt,Other,2020-01-01,y,dos,linux,,2020-01-01,2020-01-01,0,OSVDB-2,\n")
MSF = {"exploit_multi/http/log4shell": {"fullname": "exploit/multi/http/log4shell_header_injection",
                                        "name": "Log4Shell HTTP Header Injection", "type": "exploit", "rank": 600,
                                        "disclosure_date": "2021-12-09",
                                        "references": ["CVE-2021-44228", "URL-https://example.org"]}}
NVD = {"vulnerabilities": [{"cve": {"vulnStatus": "Analyzed", "published": "2021-12-10T10:15:09.143",
                                    "descriptions": [{"lang": "en", "value": "Apache Log4j2 JNDI features..."}],
                                    "weaknesses": [{"description": [{"value": "CWE-502"}, {"value": "NVD-CWE-Other"}]}],
                                    "metrics": {"cvssMetricV31": [{"type": "Primary", "cvssData": {
                                        "version": "3.1", "baseScore": 10.0, "baseSeverity": "CRITICAL",
                                        "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H"}}]},
                                    "cisaExploitAdd": "2021-12-10"}}]}
MSRC = {"value": [{"cveNumber": "CVE-2021-44228", "cveTitle": "Log4j", "exploited": "Exploitation Detected",
                   "publiclyDisclosed": "Yes", "isMariner": False, "releaseDate": "2021-12-11T00:00:00Z"}]}
VULNRICH = {"containers": {"adp": [{"metrics": [{"other": {"type": "ssvc", "content": {"options": [
    {"Exploitation": "active"}, {"Automatable": "yes"}, {"Technical Impact": "total"}]}}}]}]}}
EPSS = {"data": [{"cve": "CVE-2021-44228", "epss": "0.97", "percentile": "0.999", "date": "2026-10-03"}]}


class R:
    def __init__(self, obj=None, text=None, status=200):
        self._obj, self.text, self.status_code = obj, text if text is not None else json.dumps(obj), status

    def json(self):
        return self._obj

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class Web:
    def __init__(self, fail=()):
        self.urls, self.fail = [], set(fail)

    def get(self, url, timeout=None, headers=None):
        self.urls.append(url)
        for key, resp in (("known_exploited", R(KEV)), ("files_exploits", R(text=EDB)),
                          ("modules_metadata", R(MSF)), ("services.nvd", R(NVD)), ("api.msrc", R(MSRC)),
                          ("vulnrichment", R(VULNRICH)), ("api.first.org", R(EPSS))):
            if key in url:
                if key in self.fail:
                    raise ConnectionError("down")
                if "vulnrichment" in url and "44228" not in url:
                    return R({}, status=404)
                return resp
        raise AssertionError(url)


def test_every_source_is_read_and_combined(tmp_path):
    web = Web()
    out = threatintel.enrich(tmp_path, ["CVE-2021-44228", "CVE-2020-1234", "GHSA-xxxx"], session=web, log=lambda m: None,
                             sleep=lambda s: None)
    assert set(out) == {"CVE-2021-44228", "CVE-2020-1234"}                 # GHSA ids aren't CVE look-ups
    i = out["CVE-2021-44228"]
    assert i["kev"]["ransomware"] == "Known" and i["kev"]["due"] == "2021-12-24"
    assert i["exploitdb"][0]["id"] == "50592" and i["exploitdb"][0]["verified"]
    assert i["metasploit"][0]["module"] == "exploit/multi/http/log4shell_header_injection"
    assert i["nvd"]["score"] == 10.0 and i["nvd"]["cwes"] == ["CWE-502"] and i["nvd"]["severity"] == "Critical"
    assert i["msrc"]["tracked"] and i["msrc"]["exploited"] == "Exploitation Detected"
    assert i["vulnrichment"] == {"covered": True, "exploitation": "active", "automatable": "yes",
                                 "technical_impact": "total"}
    assert i["epss"]["score"] == 0.97
    assert out["CVE-2020-1234"]["vulnrichment"] == {"covered": False}
    prio, reasons = threatintel.assess(i, "Critical")
    assert prio == "Act now"
    text = " ".join(reasons)
    for word in ("CISA lists it as exploited", "ransomware", "Microsoft reports it exploited", "Metasploit",
                 "Exploit-DB", "EPSS", "exploitation is active"):
        assert word in text
    links = [lk["url"] for lk in threatintel.sources_used({**i, "cve": "CVE-2021-44228"})]
    assert "https://nvd.nist.gov/vuln/detail/CVE-2021-44228" in links
    assert "https://www.exploit-db.com/exploits/50592" in links
    assert any("log4shell_header_injection.rb" in u for u in links)
    st = threatintel.status(tmp_path)
    assert st["kev"]["entries"] == 1 and st["exploitdb"]["entries"] == 1 and st["metasploit"]["entries"] == 1


def test_catalogs_and_cve_answers_are_cached(tmp_path):
    threatintel.enrich(tmp_path, ["CVE-2021-44228"], session=Web(), log=lambda m: None, sleep=lambda s: None)
    web = Web()
    threatintel.enrich(tmp_path, ["CVE-2021-44228"], session=web, log=lambda m: None, sleep=lambda s: None)
    assert web.urls == []                                                  # nothing downloaded again


def test_a_failing_source_is_noted_and_the_rest_still_work(tmp_path):
    out = threatintel.enrich(tmp_path, ["CVE-2021-44228"], session=Web(fail={"services.nvd", "known_exploited"}),
                             log=lambda m: None, sleep=lambda s: None)
    i = out["CVE-2021-44228"]
    assert i["nvd"] is None and i["kev"] is None and i["metasploit"]
    st = threatintel.status(tmp_path)
    assert "ConnectionError" in st["nvd"]["error"] and "ConnectionError" in st["kev"]["error"]
    assert threatintel.assess(i, "Critical")[0] == "Act now"                # Metasploit alone is enough


def test_priority_levels_and_tooling():
    assert threatintel.assess({}, "Moderate") == ("Routine", ["No source reports exploitation or public exploit code."])
    assert threatintel.assess({}, "High")[0] == "Soon"
    assert threatintel.assess({"exploitdb": [{"id": "1"}]}, "Low")[0] == "Soon"
    assert threatintel.assess({"vulnrichment": {"exploitation": "poc"}}, "Low")[0] == "Soon"
    assert threatintel.assess({"epss": {"score": 0.02, "percentile": 0.8}}, "Low")[0] == "Routine"
    prio, why = threatintel.assess({"kev": {"added": "2024-01-01"}}, "High", tooling=True)
    assert prio == "Soon" and "only used to install software" in why[-1]


def test_findings_are_ranked_by_exploitation(tmp_path, monkeypatch):
    monkeypatch.setenv("WEPA_THREAT_INTEL", "1")
    monkeypatch.setattr(threatintel, "enrich", lambda d, cves, **k: {
        "CVE-2021-44228": {"kev": {"added": "2021-12-10"}, "metasploit": [{"module": "m"}], "exploitdb": []}})
    fs = [vulns.Finding("aaa", "1", ["CVE-2020-0001"], "Critical", "x", "2"),
          vulns.Finding("zzz", "1", ["CVE-2021-44228", "GHSA-x"], "Moderate", "y", "2")]
    vulns.add_intel(tmp_path, fs)
    assert [f.package for f in fs] == ["zzz", "aaa"]                       # exploited first, despite severity
    assert fs[0].priority == "Act now" and fs[1].priority == "Soon"


def test_owasp_checklist_covers_all_ten_with_evidence(tmp_path, monkeypatch):
    monkeypatch.delenv("WEPA_BASIC_AUTH", raising=False)
    items = owasp.checklist(tmp_path)
    assert [i.code for i in items] == [f"A{n:02d}" for n in range(1, 11)]
    assert items[2].name == "Software Supply Chain Failures" and items[9].name == "Mishandling of Exceptional Conditions"
    assert all(i.controls and all(ev for _, ev in i.controls) for i in items)
    assert all(i.url.startswith("https://owasp.org/Top10/2025/A") for i in items)
    a01 = items[0]
    assert a01.status == "Gap"                                             # sign-in off on this copy
    monkeypatch.setenv("WEPA_BASIC_AUTH", "x:y")
    a07 = owasp.checklist(tmp_path)[6]
    assert a07.status == "Partial" and any("multi-factor" in g for g in a07.gaps)
