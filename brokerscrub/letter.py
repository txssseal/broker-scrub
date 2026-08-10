"""Deletion demand generator — cites TDPSA (Texas) and CCPA (California)."""

from .config import Identity


def subject(identity: Identity, token: str) -> str:
    # No legal name in the subject: it rides cleartext across every relay and to
    # every registrant (incl. sham brokers). The name is in the body where it's
    # needed to locate records.
    return f"Personal Data Deletion Request (TDPSA / CCPA) [Ref: {token}]"


def body(identity: Identity, broker_name: str, token: str) -> str:
    emails = "\n".join(f"  - {e}" for e in identity.emails) or "  - (none provided)"
    phones = "\n".join(f"  - {p}" for p in identity.phones)
    addresses = "\n".join(f"  - {a}" for a in identity.addresses)

    aliases_line = ""
    if identity.aliases:
        aliases_line = ("I may also appear in your records under these names: "
                        + "; ".join(identity.aliases) + "\n")

    identifiers = f"Email addresses:\n{emails}\n"
    if phones:
        identifiers += f"Phone numbers:\n{phones}\n"
    if addresses:
        identifiers += f"Current and former addresses:\n{addresses}\n"
    if identity.dob:
        identifiers += f"Date of birth: {identity.dob}\n"

    return f"""To the Privacy Team at {broker_name}:

I am a consumer residing in the state of {identity.state}. Your company is a
registered data broker under the California Delete Act (SB 362). I am
exercising my statutory rights to demand deletion of my personal information:

  1. DELETE all personal information you hold about me, pursuant to
     Tex. Bus. & Com. Code § 541.051(b)(4) (Texas Data Privacy and Security
     Act) and, to the extent applicable, Cal. Civ. Code § 1798.105
     (CCPA/CPRA), including data held by your service providers, contractors,
     and affiliates (Cal. Civ. Code § 1798.105(c)(1)).

  2. DO NOT SELL OR SHARE my personal information, pursuant to
     Tex. Bus. & Com. Code § 541.051(b)(5) and Cal. Civ. Code § 1798.120.

  3. DO NOT RETAIN any of my personal information except as strictly
     required by law, and do not re-collect or re-derive my records from
     public or third-party sources following this deletion.

  4. CONFIRM completion of this request in writing, by reply to this email,
     within 45 days as required by Tex. Bus. & Com. Code § 541.055(a) and
     Cal. Civ. Code § 1798.130(a)(2).

The following identifiers are provided solely to locate my records and are
themselves subject to this deletion demand:

Full name: {identity.full_name}
{aliases_line}{identifiers}
Authentication and method of submission: the identifiers above are sufficient
to authenticate me using commercially reasonable efforts. Under Tex. Bus. &
Com. Code § 541.051(b) you shall comply with an authenticated request to delete
and to opt out. You may decline only if you are "unable to authenticate the
request using commercially reasonable efforts," in which case § 541.052(e)
requires you to identify the SPECIFIC additional information reasonably
necessary to authenticate me — you may not substitute a self-service portal for
a response, and you "may not require a consumer to create a new account to
exercise the consumer's rights" (§ 541.055(b)). Any demand exceeding what is
reasonably necessary to authenticate (for example, a notarized affidavit) is
not authorized by the statute. Your response is due "not later than the 45th
day after the date of receipt of the request" (§ 541.052(b)); that deadline
runs from receipt of this email and is not reset by redirecting me elsewhere.

If this request is not honored within the statutory period, I will invoke the
appeal process you are required to maintain (§ 541.053(a)) and, upon denial,
use the mechanism you must provide to submit a complaint to the Texas Attorney
General (§ 541.053(d)). The Attorney General has exclusive authority to enforce
this chapter (§ 541.151); a violation not cured within 30 days (§ 541.154) is
subject to a civil penalty of up to $7,500 per violation (§ 541.155(a)). To the
extent I am also covered by the CCPA/CPRA, I assert the parallel rights and
verification limits thereunder.

Reference: {token} — please include this reference in all correspondence.

{identity.full_name}
"""


def followup_subject(original_subject: str) -> str:
    s = original_subject or "Personal Data Deletion Request"
    return s if s.lower().startswith("re:") else f"Re: {s}"


def followup_body(identity: Identity, broker_name: str, token: str) -> str:
    """Firm reply to a broker that deflected to a portal / demanded an account.
    TX-resident posture: TDPSA is binding; CCPA only as persuasive framing."""
    ids = [f"full legal name ({identity.full_name})"]
    if identity.aliases:
        ids.append("names also used: " + "; ".join(identity.aliases))
    if identity.emails:
        ids.append("email(s): " + ", ".join(identity.emails))
    if identity.phones:
        ids.append("phone(s): " + ", ".join(identity.phones))
    if identity.addresses:
        ids.append("current/former address(es): " + "; ".join(identity.addresses))
    if identity.dob:
        ids.append(f"date of birth: {identity.dob}")
    id_block = "\n".join(f"  - {x}" for x in ids)

    return f"""To the Privacy Team at {broker_name}:

Your response directs me to a self-service portal / identity-verification step.
I decline to treat that redirect as satisfying your obligations under the Texas
Data Privacy and Security Act (Tex. Bus. & Com. Code ch. 541), which governs me
as a Texas resident, and I will not create a new account.

1. My request is already authenticated. I have provided identifiers sufficient
   to authenticate me using commercially reasonable efforts:
{id_block}
   Under § 541.051(b) you "shall comply with an authenticated consumer request"
   to delete personal data and to opt out of processing.

2. If you contend you cannot authenticate me, § 541.052(e) requires you to
   identify the SPECIFIC "additional information reasonably necessary to
   authenticate" — not route me into a portal or account as a substitute. I
   will supply information reasonably necessary to authenticate. I will not
   provide a notarized affidavit or documentation beyond what is needed to
   match me to my data, which exceeds § 541.052(e).

3. You "may not require a consumer to create a new account to exercise the
   consumer's rights under this subchapter" (§ 541.055(b)). I have no existing
   account with you; you may not condition my rights on creating one.

4. A portal redirect is not a response and does not stop the clock. Your
   deadline runs "not later than the 45th day after the date of receipt of the
   request" (§ 541.052(b)), measured from my original request, not from today.

Action required: process my deletion and opt-out request as submitted, or,
within your remaining § 541.052(b) window, specify in writing the exact
additional information reasonably necessary to authenticate me. A generic
instruction to use a portal or create an account satisfies neither.

If you refuse, provide the appeal process required by § 541.053(a); upon denial
you must provide the mechanism under § 541.053(d)/§ 541.152 to submit a
complaint to the Texas Attorney General, and I will file one. Enforcement is
exclusive to the Attorney General (§ 541.151); an uncured violation
(§ 541.154) carries a civil penalty of up to $7,500 per violation
(§ 541.155(a)).

Reference: {token} — please retain this reference in all correspondence.

{identity.full_name}
"""
