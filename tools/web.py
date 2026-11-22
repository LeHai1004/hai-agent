from search import web_search as raw_search


def web_search(query: str) -> str:
    """Search the web using DuckDuckGo to get current information on a query."""
    print(f"  [tool] web_search: {query}")
    results = raw_search(query)
    if not results:
        return "No results found."
    blocks = []
    for i, r in enumerate(results, 1):
        blocks.append(
            f"[{i}] {r.get('title', '')}\n{r.get('body', '')}\n"
            f"Nguồn: {r.get('href', '')}"
        )
    return "\n\n".join(blocks)
