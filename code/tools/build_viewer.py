"""Build a single-file HTML results viewer from output.csv alone.

    python code/tools/build_viewer.py                 # reads ./output.csv, writes ./viewer/index.html
    python code/tools/build_viewer.py --input other.csv --out viewer/index.html

Only the prediction file is read. No dataset files, request texts, names or emails are embedded.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ghost Data Audit</title>
<style>
:root{--bg:#f6f7fb;--card:#ffffff;--ink:#1b1f2a;--muted:#6b7280;--line:#e5e7eb;--accent:#2f5bea;
--complete:#1a9c6b;--incomplete:#d9480f;--na:#6b7280;--find:#b42318;--rem:#b54708;--ver:#026aa2;--chip:#eef1f8}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#0f1218;--card:#171b24;--ink:#e8eaf0;--muted:#9aa3b2;--line:#2a3040;--chip:#222838}}
:root[data-theme="dark"]{--bg:#0f1218;--card:#171b24;--ink:#e8eaf0;--muted:#9aa3b2;--line:#2a3040;--chip:#222838}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:28px 16px 8px;max-width:1200px;margin:0 auto}
h1{margin:0 0 4px;font-size:26px}header p{margin:0;color:var(--muted)}
main{max-width:1200px;margin:0 auto;padding:8px 16px 40px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:16px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.tile .n{font-size:28px;font-weight:700}.tile .l{color:var(--muted);font-size:13px}
.tile.complete .n{color:var(--complete)}.tile.incomplete .n{color:var(--incomplete)}.tile.na .n{color:var(--na)}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0 16px}
.chip{border:1px solid var(--line);background:var(--chip);border-radius:999px;padding:6px 12px;cursor:pointer;font-size:13px}
.chip.on{background:var(--accent);color:#fff;border-color:var(--accent)}
input[type=search]{flex:1;min-width:200px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
.grid{display:grid;grid-template-columns:minmax(260px,340px) 1fr;gap:16px}
@media (max-width:820px){.grid{grid-template-columns:1fr}}
.list{background:var(--card);border:1px solid var(--line);border-radius:12px;max-height:70vh;overflow:auto}
.row{display:flex;justify-content:space-between;gap:8px;padding:10px 14px;border-bottom:1px solid var(--line);cursor:pointer}
.row:hover,.row.sel{background:var(--chip)}.row .id{font-weight:600}.row .meta{color:var(--muted);font-size:12px}
.badge{font-size:11px;font-weight:700;letter-spacing:.3px;padding:2px 8px;border-radius:999px;color:#fff}
.badge.COMPLETE{background:var(--complete)}.badge.INCOMPLETE{background:var(--incomplete)}.badge.NOT_APPLICABLE{background:var(--na)}
.detail{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;min-height:300px}
.detail h2{margin:0 0 4px;font-size:20px}.detail .sub{color:var(--muted);margin-bottom:12px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:8px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.4px}
.ph{font-weight:700;font-size:12px}.ph.FINDING{color:var(--find)}.ph.REMEDIATION{color:var(--rem)}.ph.VERIFICATION{color:var(--ver)}
code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:13px}
.empty{color:var(--muted);padding:40px;text-align:center}
.bars{margin-top:16px}.bar{display:grid;grid-template-columns:170px 1fr 48px;gap:8px;align-items:center;margin:6px 0;font-size:13px}
.bar i{display:block;height:10px;border-radius:5px;background:var(--find)}
footer{max-width:1200px;margin:0 auto;padding:0 16px 24px;color:var(--muted);font-size:12px}
</style>
</head>
<body>
<header>
  <h1>Ghost Data Still Exists: audit results</h1>
  <p>One row per deletion request. Built from <code>output.csv</code> only. Click a case to see its findings, remediation plan and verification checks.</p>
</header>
<main>
  <div class="tiles" id="tiles"></div>
  <div class="controls" id="controls">
    <span class="chip on" data-f="ALL">All</span>
    <span class="chip" data-f="COMPLETE">Complete</span>
    <span class="chip" data-f="INCOMPLETE">Incomplete</span>
    <span class="chip" data-f="NOT_APPLICABLE">Not applicable</span>
    <input type="search" id="q" placeholder="Search case id, system, resource, operation or evidence id">
  </div>
  <div class="grid">
    <div class="list" id="list"></div>
    <div class="detail" id="detail"><div class="empty">Select a case</div></div>
  </div>
  <div class="bars" id="bars"></div>
</main>
<footer>Phases: <span class="ph FINDING">FINDING</span> what is still there · <span class="ph REMEDIATION">REMEDIATION</span> what to do, in safe order · <span class="ph VERIFICATION">VERIFICATION</span> what must be true afterwards. Findings and complete-case checks cite the evidence file that proves them.</footer>
<script>
const DATA = __DATA__;
const META = __META__;
const $ = s => document.querySelector(s);
let filter = 'ALL', query = '', selected = null;
function natKey(id){const m=id.match(/(\d+)$/);return m?parseInt(m[1],10):0}
DATA.sort((a,b)=>natKey(a.case_id)-natKey(b.case_id));
function tiles(){
  const c = META.counts;
  $('#tiles').innerHTML = [
    ['', META.total, 'requests audited'],
    ['complete', c.COMPLETE||0, 'complete'],
    ['incomplete', c.INCOMPLETE||0, 'incomplete'],
    ['na', c.NOT_APPLICABLE||0, 'not a deletion request'],
    ['', META.findings, 'findings'],
    ['', META.steps, 'steps in total'],
  ].map(([cls,n,l])=>`<div class="tile ${cls}"><div class="n">${n}</div><div class="l">${l}</div></div>`).join('');
}
function matches(r){
  if (filter!=='ALL' && r.deletion_status!==filter) return false;
  if (!query) return true;
  const q = query.toLowerCase();
  if (r.case_id.toLowerCase().includes(q) || r.deletion_status.toLowerCase().includes(q)) return true;
  return r.steps.some(s=>[s.system_id,s.resource_id,s.operation,s.state,s.evidence_id].join(' ').toLowerCase().includes(q));
}
function list(){
  const rows = DATA.filter(matches);
  $('#list').innerHTML = rows.length ? rows.map(r=>{
    const f = r.steps.filter(s=>s.phase==='FINDING').length;
    const meta = r.deletion_status==='NOT_APPLICABLE' ? 'no deletion requested' : `${f} finding${f===1?'':'s'} · ${r.steps.length} step${r.steps.length===1?'':'s'}`;
    return `<div class="row ${selected===r.case_id?'sel':''}" data-id="${r.case_id}"><div><div class="id">${r.case_id}</div><div class="meta">${meta}</div></div><span class="badge ${r.deletion_status}">${r.deletion_status.replace('_',' ')}</span></div>`;
  }).join('') : '<div class="empty">No cases match</div>';
  document.querySelectorAll('.row').forEach(el=>el.onclick=()=>{selected=el.dataset.id;list();detail();});
}
function detail(){
  const r = DATA.find(x=>x.case_id===selected);
  if(!r){$('#detail').innerHTML='<div class="empty">Select a case</div>';return;}
  const sub = r.deletion_status==='NOT_APPLICABLE' ? 'The request does not ask for deletion, so there is nothing to audit.' :
    r.deletion_status==='COMPLETE' ? 'Every relevant record already has the required treatment. The checks below cite the evidence that proves it.' :
    'Data or recreation paths remain. Findings cite evidence; the plan and the post-remediation checks are future work (no evidence id).';
  const rows = r.steps.map(s=>`<tr><td>${s.step_no}</td><td class="ph ${s.phase}">${s.phase}</td><td>${s.operation}</td><td><code>${s.system_id}</code></td><td><code>${s.resource_id}</code></td><td>${s.state}</td><td>${s.evidence_id?`<code>${s.evidence_id}</code>`:'<span style="color:var(--muted)">—</span>'}</td></tr>`).join('');
  $('#detail').innerHTML = `<h2>${r.case_id} <span class="badge ${r.deletion_status}">${r.deletion_status.replace('_',' ')}</span></h2><div class="sub">${sub}</div>` +
    (r.steps.length ? `<table><thead><tr><th>#</th><th>Phase</th><th>Operation</th><th>System</th><th>Resource</th><th>State</th><th>Evidence</th></tr></thead><tbody>${rows}</tbody></table>` : '<div class="empty">No steps (empty JSON array)</div>');
}
function bars(){
  const sys = META.findings_by_system; const max = Math.max(1,...Object.values(sys));
  const entries = Object.entries(sys).sort((a,b)=>b[1]-a[1]);
  $('#bars').innerHTML = '<h3 style="margin:24px 0 8px;font-size:16px">Where the leftover data was found</h3>' + entries.map(([k,v])=>`<div class="bar"><span><code>${k}</code></span><i style="width:${Math.round(100*v/max)}%"></i><span>${v}</span></div>`).join('');
}
document.querySelectorAll('.chip').forEach(ch=>ch.onclick=()=>{document.querySelectorAll('.chip').forEach(c=>c.classList.remove('on'));ch.classList.add('on');filter=ch.dataset.f;list();});
$('#q').oninput=e=>{query=e.target.value.trim();list();};
tiles();list();bars();
if (DATA.length){selected = (DATA.find(r=>r.deletion_status==='INCOMPLETE' && r.steps.length>=8)||DATA[0]).case_id; list(); detail();}
</script>
</body>
</html>
"""


def build(input_path: Path, out_path: Path) -> dict:
    rows = []
    with input_path.open("r", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            steps = json.loads(r["steps_json"] or "[]")
            steps.sort(key=lambda s: int(s.get("step_no", 0)))
            rows.append({"case_id": r["case_id"], "deletion_status": r["deletion_status"], "steps": steps})
    counts = Counter(r["deletion_status"] for r in rows)
    findings = [s for r in rows for s in r["steps"] if s.get("phase") == "FINDING"]
    meta = {
        "total": len(rows),
        "counts": dict(counts),
        "steps": sum(len(r["steps"]) for r in rows),
        "findings": len(findings),
        "findings_by_system": dict(Counter(s.get("system_id", "") for s in findings)),
    }
    html = (TEMPLATE
            .replace("__DATA__", json.dumps(rows, separators=(",", ":"), ensure_ascii=False))
            .replace("__META__", json.dumps(meta, separators=(",", ":"))))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return meta


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", default=str(ROOT / "output.csv"))
    p.add_argument("--out", default=str(ROOT / "viewer" / "index.html"))
    a = p.parse_args(argv)
    meta = build(Path(a.input), Path(a.out))
    print(f"wrote {a.out}: {meta['total']} cases, {meta['steps']} steps, {meta['findings']} findings, "
          f"{Path(a.out).stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
