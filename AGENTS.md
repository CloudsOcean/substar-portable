# Substar agent API discovery

When operating a live Substar subtitle/video project, read the adjacent
`SKILL.md` in this project root. It contains the complete Substar skill.

Resolve the service origin and project ID from the user's editor URL. Start with
`GET <origin>/api/agent`, then read its returned guide, operation reference and
OpenAPI entry points. If no address was supplied, the known local discovery URL is
`http://127.0.0.1:8769/api/agent`. Do not assume a supplied URL uses that port.

The calling agent does editorial reasoning with its own model by default. Program
model routes require the user's explicit selection. Read effective kept content
and current revisions; apply changes through versioned APIs. API details live in
the running service, not in a duplicated operation catalog here.

This operational entry point applies to project work. For Substar source-code
development, follow the applicable repository instructions and implementation.
