"""Multi-tenant DSAR / support platforms.

Brokers list privacy-request webforms hosted on shared platforms (OneTrust,
Zendesk, etc.). The registrable domain of such a host (e.g. onetrust.com) is
NOT owned by the broker — anyone can stand up a page on a subdomain. So we
must never allowlist the bare eTLD+1 of these platforms; we store and match
the FULL host instead. Both registry.py (when building the allowlist) and
verifier.py (when matching a link) consult this set.
"""

MULTITENANT_PLATFORMS = {
    "onetrust.com",
    "cookielaw.org",        # OneTrust CDN/webform
    "trustarc.com",
    "trustarc.eu",
    "zendesk.com",
    "usepylon.com",
    "service-now.com",
    "servicenow.com",
    "securiti.ai",
    "transcend.io",
    "wirewheel.io",
    "datagrail.io",
    "typeform.com",
    "jotform.com",
    "formstack.com",
    "google.com",           # docs.google.com / forms.gle webforms
    "googleusercontent.com",
    "surveymonkey.com",
    "hubspot.com",          # hubspotusercontent / forms
    "hsforms.com",
    "salesforce.com",
    "force.com",
    "microsoft.com",
    "office.com",
    "powerappsportals.com",
    "zoho.com",
    "zohopublic.com",
}


def is_multitenant(registrable_domain: str) -> bool:
    return registrable_domain in MULTITENANT_PLATFORMS
