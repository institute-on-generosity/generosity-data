#!/usr/bin/env python3
"""Turn docs/shared-database-brief.html into the README's "Brief" section (Markdown, collapsible per tab).

  python3 etl/brief_to_md.py > /tmp/brief.md     # then replace the README section from "## Brief" to "## Local setup"
"""
import os, re, html, sys
s=open(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "docs", "shared-database-brief.html")).read()
def txt(x): return html.unescape(re.sub(r"\s+"," ",re.sub(r"<[^>]+>","",x))).strip()
def cell(x):
    x=re.sub(r'<span class="tip"[^>]*>.*?</span></span>','</span>', x, flags=re.S)
    x=re.sub(r'<a href="([^"]+)">(.*?)</a>', lambda m: f"[{txt(m.group(2))}]({m.group(1)})", x, flags=re.S)
    x=re.sub(r"<b>(.*?)</b>", lambda m: f"**{txt(m.group(1))}**", x, flags=re.S)
    x=re.sub(r"<code>(.*?)</code>", lambda m: f"`{txt(m.group(1))}`", x, flags=re.S)
    return txt(x).replace("|","/")
def table(block, header=None):
    rows=re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.S)
    out=[]
    for r in rows:
        cells=re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)
        if 'class="grp"' in r or len(cells)==1: out.append(("grp",[cell(c) for c in cells])); continue
        out.append(("th" if "<th" in r else "td",[cell(c) for c in cells]))
    ths=[c for k,c in out if k=="th"]
    n=max(len(c) for k,c in out if k!="grp")
    head=[h or " " for h in (ths[0] if ths else (header or [" "]*n))]
    lines=["| "+" | ".join(head)+" |","|"+"---|"*len(head)]
    for k,c in out:
        if k=="th": continue
        if k=="grp": lines.append("| **"+c[0]+"** |"+" |"*(len(head)-1)); continue
        lines.append("| "+" | ".join(c)+" |")
    return "\n".join(lines)
def ul(block): return "\n".join("- "+cell(li) for li in re.findall(r"<li>(.*?)</li>", block, re.S))
def tbl_in(b): return b[b.index("<table>"):b.index("</table>")]
db=s[s.index('id="p-db"'):s.index('id="p-src"')]
cloud=s[s.index('id="p-cloud"'):s.index('id="p-ins"')]
ins=s[s.index('id="p-ins"'):]
src=s[s.index('id="p-src"'):s.index('id="p-cloud"')]
def sec(panel, title):
    i=panel.index(f"<h2>{title}</h2>"); j=panel.find("<h2>", i+5)
    return panel[i: j if j>0 else len(panel)]
def mermaid(b):
    m=re.search(r'<pre class="mermaid">(.*?)</pre>', b, re.S); return "```mermaid"+html.unescape(m.group(1)).rstrip()+"\n```"
o=[]
o.append("## Brief: one database for our 990 projects\n")
o.append("Written for the Oct 14, 2026 meeting with Tyler (Philanthro Map), kept current. Same content as an interactive page: [`docs/shared-database-brief.html`](docs/shared-database-brief.html) (download and open in a browser) or the [claude.ai brief](https://claude.ai/artifact/C9ijG2YA29zEoDEM9rs4Bf) (shared on request).\n")
stats=re.findall(r'<div class="stat"><b>(.*?)</b><span>(.*?)</span></div>', db)
o.append("| "+" | ".join(txt(b) for _,b in stats)+" |\n|"+"---|"*len(stats)+"\n| "+" | ".join(f"**{txt(a)}**" for a,_ in stats)+" |\n")
o.append(ul(db[db.index("<ul>"):db.index("</ul>")])+"\n")
o.append("<details><summary><b>Database</b>: how data flows, what we keep and leave out, tables, vector search, limits</summary>\n")
f=sec(db,"How data flows"); o.append("### How data flows\n\n"+mermaid(f)+"\n\n"+ul(f[f.index("<ul>"):f.index("</ul>")])+"\n")
k=sec(db,"What we keep from a 990"); o.append("### What we keep from a 990\n\nA typical 990 fills in **~440** values (median of 254 filings; 990-EZ ~160, 990-PF ~400). We keep about 40.\n\n"+table(tbl_in(k))+"\n")
l=sec(db,"What we leave out"); o.append("### What we leave out\n\n★ = worth adding next\n\n"+table(tbl_in(l))+"\n")
t=sec(db,"Tables"); o.append("### Tables\n\n"+mermaid(t)+"\n\n"+table(tbl_in(t), header=["Table","Holds"])+"\n")
v=sec(db,"Vector search"); o.append("### Vector search\n\n"+mermaid(v)+"\n\n"+ul(v[v.index("<ul>"):v.index("</ul>")])+"\n")
p=sec(db,"For Philanthro Map"); o.append("### For Philanthro Map\n\n**90% of grants map to a county:** `grants.recipient_zip → zip_regions → county_need`.\n\n"+ul(p[p.index("<ul>"):p.index("</ul>")])+"\n")
o.append("### Limits\n\n"+ul(sec(db,"Limits"))+"\n")
o.append("### Decide\n\n"+"\n".join(f"{i+1}. {txt(h)}" for i,h in enumerate(re.findall(r"<h3>(.*?)</h3>", sec(db,"Decide"))))+"\n")
o.append("</details>\n")
o.append("<details><summary><b>Data sources</b>: connected, coming, keeping it fresh</summary>\n\nEvery source by what it brings, with API links and status: [`docs/data-sources.md`](docs/data-sources.md).\n")
fr=sec(src,"Keeping it fresh"); o.append("### Keeping it fresh\n\n"+table(tbl_in(fr))+"\n\n**Nightly check:** "+" → ".join(txt(h) for h in re.findall(r"<h4>(.*?)</h4>", fr))+".\n")
o.append("</details>\n")
o.append("<details><summary><b>Cloud plan</b>: now, next, ultimate; stages; how big; where embeddings run</summary>\n")
heads=re.findall(r'<div class="ph-head"[^>]*><h4>(.*?)</h4><span>(.*?)</span><b>(.*?)</b></div>', cloud)
cells=re.findall(r'<div class="ph-cell[^"]*" style="grid-column:(\d);grid-row:(\d)"><i>(.*?)</i>(.*?)</div>', cloud)
rows={}
for c,r,label,val in cells: rows.setdefault((int(r),label),{})[int(c)]=txt(val)
o.append("### Now, next, ultimate\n\n| | "+" | ".join(f"**{txt(h)}**: {txt(sub)}, {txt(cost)}" for h,sub,cost in heads)+" |\n|---|---|---|---|")
for (r,label),vv in sorted(rows.items()): o.append(f"| **{label}** | {vv.get(1,'')} | {vv.get(2,'')} | {vv.get(3,'')} |")
o.append("")
mv=sec(cloud,"Moving to the cloud")
stages=re.findall(r'<li class="stage[^"]*">\s*<div class="stage-head"><h4>(.*?)</h4>(?:<span class="where">(.*?)</span>)?</div>\s*<p>(.*?)</p>\s*<ul class="chips">(.*?)</ul>', mv, re.S)
o.append("### Moving to the cloud\n")
for i,(t_,w,p_,chips) in enumerate(stages,1):
    names=[txt(re.sub(r'<span class="q".*?</span></span>','',c, flags=re.S)) for c in re.findall(r"<li>(.*?)</li>", chips, re.S)]
    o.append(f"{i}. **{txt(t_)}**{' ('+txt(w)+')' if w else ''}: {txt(p_).rstrip('.')}. Uses: {', '.join(names)}.")
a=mv.index('<li><b>APIs'); o.append("\n"+ul(mv[a-10:mv.index("</ul>",a)])+"\n")
hb=mv[mv.index("<h3>How big it gets</h3>"):]
o.append("#### How big it gets\n\n"+table(tbl_in(hb), header=[" ","Today","Worst case"])+"\n")
hb2=hb[hb.index("</table>")+8:]; o.append("#### Where the embedding model runs\n\n"+table(tbl_in(hb2))+"\n")
o.append("#### Steps\n\n"+"\n".join(f"{i+1}. {cell(h)}" for i,h in enumerate(re.findall(r"<li><h3>(.*?)</h3></li>", mv[mv.index("<h3>Steps</h3>"):])))+"\n")
o.append("</details>\n")
o.append("<details><summary><b>Insights</b>: finding hidden patterns, what's missing, build order, tools</summary>\n")
for title in ["Finding hidden patterns","What's missing"]:
    b=sec(ins,title); o.append(f"### {title}\n\n"+table(tbl_in(b))+"\n")
o.append("### Patterns it could surface\n\n"+ul(sec(ins,"Patterns it could surface"))+"\n")
bo=sec(ins,"Build order")
st=re.findall(r'<h4>(.*?)</h4>(?:<span class="where">(.*?)</span>)?</div><p>(.*?)</p>', bo)
o.append("### Build order\n\n"+"\n".join(f"{i+1}. **{txt(t_)}**{' ('+txt(w)+')' if w else ''}: {cell(p_)}" for i,(t_,w,p_) in enumerate(st))+"\n")
tl=sec(ins,"Tools that help"); o.append("### Tools that help\n\n"+table(tbl_in(tl), header=["Tool","Use"]).replace(" Top 5"," ★")+"\n\n★ Top 5, in order: Splink, graph algorithms, dbt, read-only MCP, BERTopic.\n")
o.append("</details>\n")
print("\n".join(o))
