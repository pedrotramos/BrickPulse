"""Build six proposed SVG identities and their requested 3+4 fusion.

The original artwork is declared here. Output is an isolated Vite-served design
artifact, not part of the production entry point. Run with the standard library.
"""

from __future__ import annotations

import base64
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "frontend" / "logo-options"
INK = "#243439"
FONT = (
    ROOT / "frontend" / "node_modules" / "@fontsource-variable"
    / "instrument-sans" / "files" / "instrument-sans-latin-wght-normal.woff2"
)

OPTIONS = [
    {
        "id": "01-monograma",
        "name": "Monograma BP",
        "color": "#115e67",
        "description": "As iniciais viram assinatura: direto, compacto e reconhecível.",
        "meaning": "Uma leitura tipográfica de BrickPulse. O B e o P têm a mesma altura e um desenho próprio, com curvas firmes e espaços abertos.",
        "tradeoff": "A leitura das duas letras funciona melhor a partir de 24 px.",
        "shape": '<path fill-rule="evenodd" d="M6 12h23c13 0 21 8 21 19 0 7-3 12-9 15 8 3 12 9 12 17 0 13-9 21-23 21H6V12Zm14 13v15h8c6 0 9-3 9-8s-3-7-9-7h-8Zm0 28v18h9c7 0 11-3 11-9s-4-9-11-9h-9ZM58 12h12c15 0 24 9 24 23s-9 23-24 23v26H58V12Zm12 13v20h1c7 0 11-3 11-10s-4-10-11-10h-1Z"/>',
    },
    {
        "id": "02-encaixe",
        "name": "Encaixe",
        "color": "#414797",
        "description": "Dois módulos se conectam em uma marca arquitetônica.",
        "meaning": "Dois elos quadrados abertos representam infraestrutura e informação que se complementam. A conexão aparece também em uma só cor.",
        "tradeoff": "A associação com o nome é abstrata; a força está na silhueta.",
        "shape": '<path d="M8 8h54v34H48V22H22v26h20v14H8V8Zm46 26h34v54H34V54h14v20h26V48H54V34Z"/>',
    },
    {
        "id": "03-frequencia",
        "name": "Frequência",
        "color": "#b94b2c",
        "description": "O ritmo dos dados em um sinal simples e energético.",
        "meaning": "Cinco amplitudes desenham um pulso amostrado: a plataforma sendo observada ao longo do tempo, sem recorrer ao traçado de um eletrocardiograma.",
        "tradeoff": "É a direção mais familiar ao universo de áudio e telemetria.",
        "shape": '<rect x="8" y="37" width="12" height="22" rx="6"/><rect x="25" y="23" width="12" height="50" rx="6"/><rect x="42" y="8" width="12" height="80" rx="6"/><rect x="59" y="26" width="12" height="44" rx="6"/><rect x="76" y="36" width="12" height="24" rx="6"/>',
    },
    {
        "id": "04-foco",
        "name": "Foco",
        "color": "#17675d",
        "description": "Um olhar analítico que encontra o que realmente importa.",
        "meaning": "Um olho geométrico envolve um losango central: contexto ao redor, evidência no centro. Evoca observabilidade e atenção ao desperdício.",
        "tradeoff": "O símbolo também pode remeter a segurança e monitoramento.",
        "shape": '<path fill-rule="evenodd" d="M3 48C17 26 31 15 48 15s31 11 45 33C79 70 65 81 48 81S17 70 3 48Zm19 0c9 13 17 19 26 19s17-6 26-19c-9-13-17-19-26-19S31 35 22 48Z"/><path d="m48 34 14 14-14 14-14-14 14-14Z"/>',
    },
    {
        "id": "05-fluxo",
        "name": "Fluxo",
        "color": "#2759bf",
        "description": "Um ciclo aberto de observar, entender e otimizar.",
        "meaning": "Uma fita contínua cruza dois percursos. A interrupção no cruzamento deixa o gesto legível em monocromia e sugere movimento sem animação.",
        "tradeoff": "O laço se aproxima da família visual dos símbolos de infinito.",
        "shape": '<path d="m40 43-11-10C15 21 7 33 7 47s10 26 24 14l34-30c14-12 24 0 24 16S77 75 63 63L54 55" fill="none" stroke="currentColor" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/>',
    },
    {
        "id": "06-facetado",
        "name": "B Facetado",
        "color": "#2f424a",
        "description": "Uma letra esculpida: sólida, precisa e de alto contraste.",
        "meaning": "O B é reduzido a um bloco de arestas chanfradas, com duas aberturas que mantêm a leitura. A forma funciona como selo, ícone ou assinatura.",
        "tradeoff": "É a direção mais industrial e menos orgânica do conjunto.",
        "shape": '<path fill-rule="evenodd" d="M14 10h45l23 21v10l-10 7 10 7v10L59 86H14V10Zm16 15v16h24l10-8-10-8H30Zm0 30v16h24l10-8-10-8H30Z"/>',
    },
]

FUSION = {
    "id": "foco-frequencia",
    "name": "Foco + Frequência",
    "color": "#b94b2c",
    "description": "O olhar da opção 4 encontra o pulso da opção 3, no mesmo laranja da 3.",
    "meaning": "O olho geométrico envolve cinco amplitudes. O pulso substitui o losango original, integrando as duas ideias em um único símbolo.",
    "tradeoff": "Os detalhes do pulso são mais claros a partir de 24 px; a silhueta do olho continua sendo o elemento principal em 16 px.",
    "shape": '<path fill-rule="evenodd" d="M3 48C17 26 31 15 48 15s31 11 45 33C79 70 65 81 48 81S17 70 3 48Zm15 0c10 14 20 21 30 21s20-7 30-21c-10-14-20-21-30-21S28 34 18 48Z"/><rect x="24" y="43" width="6" height="10" rx="3"/><rect x="34" y="38" width="6" height="20" rx="3"/><rect x="44" y="31" width="8" height="34" rx="4"/><rect x="56" y="38" width="6" height="20" rx="3"/><rect x="66" y="43" width="6" height="10" rx="3"/>',
}


def svg(content: str, viewbox: str, label: str, style: str = "") -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{viewbox}" role="img" aria-labelledby="title">'
        f"<title id=\"title\">{html.escape(label)}</title>"
        f"{style}{content}</svg>\n"
    )


def build_collection(output: Path, options: list[dict[str, str]]) -> None:
    if not FONT.is_file():
        raise SystemExit("Install the frontend dependencies to embed the local font.")
    output.mkdir(parents=True, exist_ok=True)
    assets = output / "svg"
    assets.mkdir(exist_ok=True)
    encoded = base64.b64encode(FONT.read_bytes()).decode("ascii")
    font_license = (FONT.parent.parent / "LICENSE").read_text(encoding="utf-8")
    (output / "FONT-LICENSE.txt").write_text(font_license, encoding="utf-8")
    font_style = (
        f"<metadata>{html.escape(font_license)}</metadata>"
        '<defs><style>@font-face{font-family:BrickPulseLettering;'
        f'src:url("data:font/woff2;base64,{encoded}") format("woff2");'
        'font-style:normal;font-weight:400 700;}'
        'text{font-family:BrickPulseLettering,sans-serif;}</style></defs>'
    )
    manifest = []
    for number, option in enumerate(options, start=1):
        item = {**option, "number": number}
        manifest.append({key: value for key, value in item.items() if key != "shape"})
        mark = option["shape"]
        prefix = assets / option["id"]
        for suffix, color, lettering in (
            ("color", option["color"], INK),
            ("mono", INK, INK),
            ("reversed", "#ffffff", "#ffffff"),
        ):
            icon = f'<g fill="{color}" color="{color}">{mark}</g>'
            (prefix.with_name(prefix.name + f"-symbol-{suffix}.svg")).write_text(
                svg(icon, "0 0 96 96", f"BrickPulse — {option['name']}"), encoding="utf-8"
            )
            wordmark = (
                f'<g transform="translate(6 6) scale(.75)" fill="{color}" color="{color}">{mark}</g>'
                f'<text x="96" y="57" font-size="45" font-weight="650" '
                f'letter-spacing="-1.35" fill="{lettering}">BrickPulse</text>'
            )
            (prefix.with_name(prefix.name + f"-lockup-{suffix}.svg")).write_text(
                svg(wordmark, "0 0 328 84", f"BrickPulse — {option['name']}", font_style),
                encoding="utf-8",
            )
    (output / "options.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Built {len(options)} candidates, {len(options) * 6} SVG files: {output}")


def main() -> None:
    build_collection(OUTPUT, OPTIONS)
    build_collection(OUTPUT / "fusion", [FUSION])


if __name__ == "__main__":
    main()
