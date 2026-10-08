#!/usr/bin/env python3
"""Extract FLAG diligence fields (Financial, Legal and Governance) from 990 / 990-EZ e-file XML into `filing_flag`.

Reads the same files as etl/load_990_text.py (data/xml/2025 by default); only filings already in
`filing_text` are loaded (foreign key). Namespace-agnostic tags, so schema versions don't matter.
  Part I     CY/PY revenue, expenses, contributions, program revenue, salaries; CY fundraising expense
  Part VIII  GovernmentGrantsAmt (line 1e)
  Part X     cash + savings, assets, liabilities, net assets (BOY/EOY), unrestricted net assets
  Part IX    TotalFunctionalExpensesGrp: program / management / fundraising
  Part VI    board size and independence, family/business ties, policies, audit, diversion of assets
  Part IV    excess benefit, loans, grants and business with insiders
  Part VII   Form990PartVIISectionAGrp (990) or OfficerDirectorTrusteeEmplGrp (990-EZ): name, title, hours, pay
"""
import csv, io, json, os, subprocess, sys, xml.etree.ElementTree as ET

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "data", "xml", "2025")

COLS = """object_id ein tax_year form cy_revenue py_revenue cy_expenses py_expenses cy_contributions py_contributions
cy_program_revenue py_program_revenue cy_salaries py_salaries cy_fundraising_expenses government_grants cash total_assets
total_assets_boy total_liabilities total_liabilities_boy net_assets net_assets_boy unrestricted_net_assets program_expenses
management_expenses fundraising_expenses voting_members independent_members family_business_ties conflict_policy
whistleblower_policy retention_policy audited audit_committee diversion_of_assets form990_to_board excess_benefit
insider_loan grant_to_insider business_with_insiders people""".split()

def local(tag):
    return tag.rsplit("}", 1)[-1]

def kids(node, name):
    return [c for c in node if local(c.tag) == name]

def first(node, *path):
    """First descendant along a path of tag names (each step searches all descendants)."""
    for name in path:
        node = next((e for e in node.iter() if local(e.tag) == name), None)
        if node is None: return None
    return node

def num(node, *path):
    e = first(node, *path) if node is not None else None
    try: return str(int(round(float((e.text or "").strip())))) if e is not None else ""
    except ValueError: return ""

class Index:
    """First element per tag name in one pass, so each field lookup is a dict hit, not a tree walk."""
    def __init__(self, root):
        self.d = {}
        for e in root.iter(): self.d.setdefault(local(e.tag), e)
    def num(self, tag, sub=None):
        e = self.d.get(tag)
        return num(e, sub) if sub and e is not None else num(e) if e is not None else ""
    def flag(self, tag):
        e = self.d.get(tag)
        if e is None: return ""
        t = (e.text or "").strip().lower()
        return "t" if t in ("1", "true", "x") else "f" if t in ("0", "false") else ""

def txt(e):
    return " ".join((e.text or "").split()) if e is not None else ""

def people(form, f):
    out = []
    if form == "990":
        for g in (e for e in f.iter() if local(e.tag) == "Form990PartVIISectionAGrp"):
            name = txt(first(g, "PersonNm")) or txt(first(g, "BusinessNameLine1Txt"))
            if not name: continue
            role = next((r for r, t in (("officer", "OfficerInd"), ("key", "KeyEmployeeInd"), ("highest", "HighestCompensatedEmployeeInd"),
                                         ("director", "IndividualTrusteeOrDirectorInd"), ("director", "InstitutionalTrusteeInd"), ("former", "FormerOfcrDirectorTrusteeInd"))
                         if first(g, t) is not None), "")
            out.append({"name": name, "title": txt(first(g, "TitleTxt")), "hours": float(txt(first(g, "AverageHoursPerWeekRt")) or 0),
                        "pay": int(num(g, "ReportableCompFromOrgAmt") or 0), "other": int(num(g, "OtherCompensationAmt") or 0), "role": role})
    else:
        for g in (e for e in f.iter() if local(e.tag) == "OfficerDirectorTrusteeEmplGrp"):
            name = txt(first(g, "PersonNm")) or txt(first(g, "BusinessNameLine1Txt"))
            if not name: continue
            out.append({"name": name, "title": txt(first(g, "TitleTxt")), "hours": float(txt(first(g, "AverageHrsPerWkDevotedToPosRt")) or 0),
                        "pay": int(num(g, "CompensationAmt") or 0), "other": int(num(g, "EmployeeBenefitProgramAmt") or 0) + int(num(g, "ExpenseAccountOtherAllwncAmt") or 0), "role": ""})
    out.sort(key=lambda p: -(p["pay"] + p["other"]))
    return json.dumps(out[:25]) if out else ""

def parse(path):
    root = ET.parse(path).getroot()
    hdr = first(root, "ReturnHeader")
    form = txt(first(hdr, "ReturnTypeCd"))
    ein = txt(first(hdr, "Filer", "EIN")).zfill(9)  # not the preparer firm's EIN, which can come first
    year = txt(first(hdr, "TaxYr"))
    oid = os.path.basename(path).split("_")[0]
    r = dict.fromkeys(COLS, "")
    r.update(object_id=oid, ein=ein, tax_year=year, form=form)
    if form == "990":
        f = first(root, "IRS990")
        if f is None: return None
        ix = Index(f)
        for col, tag in (("cy_revenue", "CYTotalRevenueAmt"), ("py_revenue", "PYTotalRevenueAmt"), ("cy_expenses", "CYTotalExpensesAmt"),
                         ("py_expenses", "PYTotalExpensesAmt"), ("cy_contributions", "CYContributionsGrantsAmt"), ("py_contributions", "PYContributionsGrantsAmt"),
                         ("cy_program_revenue", "CYProgramServiceRevenueAmt"), ("py_program_revenue", "PYProgramServiceRevenueAmt"),
                         ("cy_salaries", "CYSalariesCompEmpBnftPaidAmt"), ("py_salaries", "PYSalariesCompEmpBnftPaidAmt"),
                         ("cy_fundraising_expenses", "CYTotalFundraisingExpenseAmt"), ("government_grants", "GovernmentGrantsAmt"),
                         ("total_assets", "TotalAssetsEOYAmt"), ("total_assets_boy", "TotalAssetsBOYAmt"),
                         ("total_liabilities", "TotalLiabilitiesEOYAmt"), ("total_liabilities_boy", "TotalLiabilitiesBOYAmt"),
                         ("net_assets", "NetAssetsOrFundBalancesEOYAmt"), ("net_assets_boy", "NetAssetsOrFundBalancesBOYAmt"),
                         ("voting_members", "VotingMembersGoverningBodyCnt"), ("independent_members", "IndependentVotingMemberCnt")):
            r[col] = ix.num(tag)
        cash = [ix.num(g, "EOYAmt") for g in ("CashNonInterestBearingGrp", "SavingsAndTempCashInvstGrp")]
        r["cash"] = str(sum(int(c) for c in cash if c)) if any(cash) else ""
        r["unrestricted_net_assets"] = ix.num("NoDonorRestrictionNetAssetsGrp", "EOYAmt") or ix.num("UnrestrictedNetAssetsGrp", "EOYAmt")
        tfe = ix.d.get("TotalFunctionalExpensesGrp")
        if tfe is not None:
            r["program_expenses"], r["management_expenses"], r["fundraising_expenses"] = (num(tfe, t) for t in ("ProgramServicesAmt", "ManagementAndGeneralAmt", "FundraisingAmt"))
        for col, tag in (("family_business_ties", "FamilyOrBusinessRlnInd"), ("conflict_policy", "ConflictOfInterestPolicyInd"),
                         ("whistleblower_policy", "WhistleblowerPolicyInd"), ("retention_policy", "DocumentRetentionPolicyInd"),
                         ("audited", "FSAuditedInd"), ("audit_committee", "AuditCommitteeInd"), ("diversion_of_assets", "MaterialDiversionOrMisuseInd"),
                         ("form990_to_board", "Form990ProvidedToGoverningBodyInd"), ("excess_benefit", "EngagedInExcessBenefitTransInd"),
                         ("insider_loan", "LoanOutstandingInd"), ("grant_to_insider", "GrantToRelatedPersonInd")):
            r[col] = ix.flag(tag)
        biz = [ix.flag(t) for t in ("BusinessRlnWithOrgMemInd", "BusinessRlnWithFamMemInd", "BusinessRlnWith35CtrlEntInd")]
        r["business_with_insiders"] = "t" if "t" in biz else "f" if "f" in biz else ""
    elif form == "990EZ":
        f = first(root, "IRS990EZ")
        if f is None: return None
        ix = Index(f)
        r.update(cy_revenue=ix.num("TotalRevenueAmt"), cy_expenses=ix.num("TotalExpensesAmt"), program_expenses=ix.num("TotalProgramServiceExpensesAmt"),
                 cy_contributions=ix.num("ContributionsGiftsGrantsEtcAmt"), cy_program_revenue=ix.num("ProgramServiceRevenueAmt"),
                 net_assets=ix.num("NetAssetsOrFundBalancesEOYAmt"), net_assets_boy=ix.num("NetAssetsOrFundBalancesBOYAmt"),
                 total_assets=ix.num("Form990TotalAssetsGrp", "EOYAmt"), total_assets_boy=ix.num("Form990TotalAssetsGrp", "BOYAmt"),
                 total_liabilities=ix.num("SumOfTotalLiabilitiesGrp", "EOYAmt"), total_liabilities_boy=ix.num("SumOfTotalLiabilitiesGrp", "BOYAmt"),
                 cash=ix.num("CashSavingsAndInvestmentsGrp", "EOYAmt"), excess_benefit=ix.flag("EngagedInExcessBenefitTransInd"))
    else:
        return None
    r["people"] = people(form, f)
    return [r[c] for c in COLS]

def main():
    out = io.StringIO(); w = csv.writer(out, lineterminator="\n")
    ok = skip = bad = 0
    for name in sorted(os.listdir(SRC)):
        if not name.endswith("_public.xml"): continue
        try:
            row = parse(os.path.join(SRC, name))
        except (ET.ParseError, ValueError):
            bad += 1; continue
        if row: w.writerow(row); ok += 1
        else: skip += 1
    print(f"parsed: {ok} filings, {skip} other forms, {bad} unparseable")
    cols = ", ".join(COLS)
    script = f"""CREATE TEMP TABLE ff_raw (LIKE filing_flag);
\\copy ff_raw ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')
""" + out.getvalue() + f"""\\.
INSERT INTO filing_flag ({cols}) SELECT {cols} FROM ff_raw WHERE object_id IN (SELECT object_id FROM filing_text)
ON CONFLICT (object_id) DO UPDATE SET {", ".join(f"{c} = EXCLUDED.{c}" for c in COLS[1:])};
SELECT count(*) FROM filing_flag;
"""
    p = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=script, text=True, capture_output=True)
    if p.returncode: sys.exit(f"psql failed: {p.stderr.strip()[:2000]}")
    print("filing_flag rows:", p.stdout.strip().splitlines()[-1])

if __name__ == "__main__":
    main()
