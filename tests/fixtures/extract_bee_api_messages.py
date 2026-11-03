"""Regenerate tests/fixtures/bee_api_messages.txt from a Bee source checkout (#549).

    python tests/fixtures/extract_bee_api_messages.py <path-to-bee-checkout> \
        > tests/fixtures/bee_api_messages.txt

Reads only the non-test .go files of pkg/api and pkg/jsonhttp/jsonhttp.go of
github.com/ethersphere/bee. Collects every fixed error message a pkg/api
handler can send:

1. string literals passed to jsonhttp.<Status>(w, "...");
2. string literals passed to the mapStructure response("...", logger, w);
3. string literals assigned to a response's Message ("Message: ..." or
   "resp.Message = ...");
4. identifiers passed to jsonhttp.<Status>(w, ident) or (w, ident.Error()),
   resolved to a string constant (ident = "..."), an errors.New("...")
   variable, a local ident := "...", or an fmt.Sprintf of string constants;
5. the text of every errors.New("...") in pkg/api, since such an error can
   reach a response when a handler passes it through;
6. the status texts of the jsonhttp helpers for 4xx and 5xx (Go's
   http.StatusText), sent when a handler passes no message.

Handlers that pass a Go error through (jsonhttp.X(w, err)) have dynamic text;
those are not collected. Messages containing a format verb are skipped.
"""
import pathlib
import re
import sys

# Go's net/http StatusText for the 4xx and 5xx constants jsonhttp uses.
GO_STATUS_TEXT = {
    "StatusBadRequest": "Bad Request", "StatusUnauthorized": "Unauthorized",
    "StatusPaymentRequired": "Payment Required", "StatusForbidden": "Forbidden",
    "StatusNotFound": "Not Found", "StatusMethodNotAllowed": "Method Not Allowed",
    "StatusNotAcceptable": "Not Acceptable",
    "StatusProxyAuthRequired": "Proxy Authentication Required",
    "StatusRequestTimeout": "Request Timeout", "StatusConflict": "Conflict",
    "StatusGone": "Gone", "StatusLengthRequired": "Length Required",
    "StatusPreconditionFailed": "Precondition Failed",
    "StatusRequestEntityTooLarge": "Request Entity Too Large",
    "StatusRequestURITooLong": "Request URI Too Long",
    "StatusUnsupportedMediaType": "Unsupported Media Type",
    "StatusRequestedRangeNotSatisfiable": "Requested Range Not Satisfiable",
    "StatusExpectationFailed": "Expectation Failed", "StatusTeapot": "I'm a teapot",
    "StatusMisdirectedRequest": "Misdirected Request",
    "StatusUnprocessableEntity": "Unprocessable Entity", "StatusLocked": "Locked",
    "StatusFailedDependency": "Failed Dependency", "StatusTooEarly": "Too Early",
    "StatusUpgradeRequired": "Upgrade Required",
    "StatusPreconditionRequired": "Precondition Required",
    "StatusTooManyRequests": "Too Many Requests",
    "StatusRequestHeaderFieldsTooLarge": "Request Header Fields Too Large",
    "StatusUnavailableForLegalReasons": "Unavailable For Legal Reasons",
    "StatusInternalServerError": "Internal Server Error",
    "StatusNotImplemented": "Not Implemented", "StatusBadGateway": "Bad Gateway",
    "StatusServiceUnavailable": "Service Unavailable",
    "StatusGatewayTimeout": "Gateway Timeout",
    "StatusHTTPVersionNotSupported": "HTTP Version Not Supported",
}

STRING = r'"((?:[^"\\]|\\.)*)"'


def main(bee):
    api = sorted(p for p in (bee / "pkg" / "api").glob("*.go") if not p.name.endswith("_test.go"))
    src = "\n".join(p.read_text(encoding="utf-8") for p in api)
    messages = set()

    # 1-3: literals.
    for pattern in (rf"jsonhttp\.\w+\(\s*\w+\s*,\s*{STRING}",
                    rf"\bresponse\(\s*{STRING}\s*,",
                    rf"\bMessage\s*:\s*{STRING}",
                    rf"\.Message\s*=\s*{STRING}"):
        messages.update(m.group(1) for m in re.finditer(pattern, src))

    # 4: identifiers, resolved.
    # An identifier can be assigned more than once (a local errorMsg in two
    # handlers); every value is kept.
    strings = {}
    for m in re.finditer(rf"^\s*(\w+)\s*(?::=|=)\s*{STRING}", src, re.M):
        strings.setdefault(m.group(1), set()).add(m.group(2))
    errors_new = {}
    for m in re.finditer(rf"(\w+)\s*=\s*errors\.New\(\s*{STRING}\s*\)", src):
        errors_new.setdefault(m.group(1), set()).add(m.group(2))
    sprintf = {}
    for m in re.finditer(rf"(\w+)\s*(?::=|=)\s*fmt\.Sprintf\(\s*{STRING}\s*,([^)]*)\)", src):
        args = [a.strip() for a in m.group(3).split(",")]
        if all(len(strings.get(a, ())) == 1 for a in args) and m.group(2).count("%s") == len(args):
            sprintf[m.group(1)] = {m.group(2) % tuple(next(iter(strings[a])) for a in args)}
    for m in re.finditer(r"jsonhttp\.\w+\(\s*\w+\s*,\s*(\w+)(?:\.Error\(\))?\s*\)", src):
        ident = m.group(1)
        for table in (errors_new, sprintf, strings):
            if ident in table:
                messages.update(table[ident])
                break

    # 5: every errors.New value.
    for values in errors_new.values():
        messages.update(values)

    # 6: status texts of the 4xx/5xx helpers.
    helpers = (bee / "pkg" / "jsonhttp" / "jsonhttp.go").read_text(encoding="utf-8")
    for m in re.finditer(r"Respond\(\s*w\s*,\s*http\.(Status\w+)", helpers):
        if m.group(1) in GO_STATUS_TEXT:
            messages.add(GO_STATUS_TEXT[m.group(1)])

    messages = sorted(t.encode().decode("unicode_escape") for t in messages
                      if t and not re.search(r"%[a-z]", t))
    print("# Fixed error messages a Bee pkg/api handler can send, one per line (#549).")
    print("# Public text from github.com/ethersphere/bee. Regenerate with:")
    print("#   python tests/fixtures/extract_bee_api_messages.py <bee-checkout> \\")
    print("#       > tests/fixtures/bee_api_messages.txt")
    print("# The method is described in that script.")
    for t in messages:
        print(t)


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1]))
