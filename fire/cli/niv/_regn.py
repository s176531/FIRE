import subprocess
import webbrowser
from pathlib import Path
from math import hypot, sqrt
from typing import Dict, Tuple, List
from dataclasses import dataclass, asdict

import click
import xmltodict
from pandas import DataFrame, Timestamp, isna

from fire.io.regneark import arkdef
import fire.cli

from . import (
    find_faneblad,
    gyldighedstidspunkt,
    niv,
    skriv_punkter_geojson,
    skriv_observationer_geojson,
    skriv_ark,
    er_projekt_okay,
)

from ._netoversigt import netanalyse


@dataclass
class Observationer:
    journal: List[str]
    sluk: List[str]
    fra: List[str]
    til: List[str]
    delta_H: List[float]
    L: List[int]
    opst: List[int]
    sigma: List[float]
    delta: List[float]
    kommentar: List[str]
    hvornår: List[Timestamp]
    T: List[float]
    sky: List[float]
    sol: List[float]
    vind: List[float]
    sigt: List[float]
    kilde: List[str]
    type: List[str]
    uuid: List[str]


@dataclass
class Arbejdssæt:
    punkt: List[int]
    fasthold: List[str]
    hvornår: List[Timestamp]
    kote: List[float]
    sigma: List[float]
    ny_kote: List[float]
    ny_sigma: List[float]
    Delta_kote: List[float]
    opløft: List[float]
    system: List[str]
    nord: List[float]
    øst: List[float]
    uuid: List[str]
    udelad: List[str]


@niv.command()
@fire.cli.default_options()
@click.argument("projektnavn", nargs=1, type=str)
def regn(projektnavn: str, **kwargs) -> None:
    """Beregn nye koter.

    Hvis der allerede er foretaget kontrolberegning udfører vi en endelig
    beregning. Valget styres via navnet på seneste oversigtsfaneblad, som
    går fra 'Punktoversigt' (skabt af 'læs_observationer'), via
    'Kontrolberegning' (der skrives ved første kald til denne funktion),
    til 'Endelig beregning' (der skrives ved efterfølgende kald).
    """
    er_projekt_okay(projektnavn)

    fire.cli.print("Så regner vi")

    # Hvis der ikke allerede findes et kontrolberegningsfaneblad, så er det en
    # kontrolberegning vi skal i gang med.
    kontrol = (
        find_faneblad(projektnavn, "Kontrolberegning", arkdef.PUNKTOVERSIGT, True)
        is None
    )

    # ...og så kan vi vælge den korrekte fanebladsprogression
    if kontrol:
        aktuelt_faneblad = "Punktoversigt"
        næste_faneblad = "Kontrolberegning"
        infiks = "-kon"
    else:
        aktuelt_faneblad = "Kontrolberegning"
        næste_faneblad = "Endelig beregning"
        infiks = ""

    # Håndter fastholdte punkter og slukkede observationer.
    observationer = find_faneblad(projektnavn, "Observationer", arkdef.OBSERVATIONER)
    punktoversigt = find_faneblad(projektnavn, "Punktoversigt", arkdef.PUNKTOVERSIGT)
    arbejdssæt = find_faneblad(projektnavn, aktuelt_faneblad, arkdef.PUNKTOVERSIGT)

    # Til den endelige beregning skal vi bruge de oprindelige observationsdatoer
    if not kontrol:
        arbejdssæt["Hvornår"] = punktoversigt["Hvornår"]

    arb_søjler = arbejdssæt.columns
    obs_søjler = observationer.columns
    # Konverter til dataklasse
    observationer = obs_til_dataklasse(observationer)
    arbejdssæt = arb_til_dataklasse(arbejdssæt)

    # Lokalisér fastholdte punkter
    fastholdte = find_fastholdte(arbejdssæt, kontrol)
    if 0 == len(fastholdte):
        fire.cli.print("Der skal fastholdes mindst et punkt i en beregning")
        raise SystemExit(1)

    # Ny netanalyse: Tag højde for slukkede observationer og fastholdte punkter.
    resultater = netanalyse(projektnavn)

    # Beregn nye koter for de ikke-fastholdte punkter...
    forbundne_punkter = tuple(sorted(resultater["Netgeometri"]["Punkt"]))
    estimerede_punkter = tuple(sorted(set(forbundne_punkter) - set(fastholdte)))
    fire.cli.print(
        f"Fastholder {len(fastholdte)} og beregner nye koter for {len(estimerede_punkter)} punkter"
    )

    # Skriv Gama-inputfil i XML-format
    skriv_gama_inputfil(projektnavn, fastholdte, estimerede_punkter, observationer)

    # Kør GNU Gama og skriv HTML rapport
    htmlrapportnavn = gama_udjævn(projektnavn, kontrol)

    # Indlæs nødvendige parametre til at skrive Gama output til xlsx
    punkter, koter, varianser, t_gyldig = læs_gama_output(projektnavn)

    # Opdater arbejdssæt med GNU Gama output
    beregning = opdater_arbejdssæt(punkter, koter, varianser, arbejdssæt, t_gyldig)
    værdier = []
    for _, værdi in asdict(beregning).items():
        værdier.append(værdi)
    beregning = DataFrame(list(zip(*værdier)), columns=arb_søjler)
    resultater[næste_faneblad] = beregning

    # ...og beret om resultaterne
    skriv_punkter_geojson(projektnavn, resultater[næste_faneblad], infiks=infiks)
    obs = []
    for _, o in asdict(observationer).items():
        obs.append(o)
    observationer = DataFrame(list(zip(*obs)), columns=obs_søjler)
    skriv_observationer_geojson(
        projektnavn,
        resultater[næste_faneblad].set_index("Punkt"),
        observationer,
        infiks=infiks,
    )
    skriv_ark(projektnavn, resultater)
    if fire.cli.firedb.config.getboolean("general", "niv_open_files"):
        webbrowser.open_new_tab(htmlrapportnavn)
        fire.cli.print("Færdig! - åbner regneark og resultatrapport for check.")
        fire.cli.åbn_fil(f"{projektnavn}.xlsx")


# -----------------------------------------------------------------------------
def obs_til_dataklasse(obs: DataFrame):
    return Observationer(
        journal=list(obs["Journal"]),
        sluk=list(obs["Sluk"]),
        fra=list(obs["Fra"]),
        til=list(obs["Til"]),
        delta_H=list(obs["ΔH"]),
        L=list(obs["L"]),
        opst=list(obs["Opst"]),
        sigma=list(obs["σ"]),
        delta=list(obs["δ"]),
        kommentar=list(obs["Kommentar"]),
        hvornår=list(obs["Hvornår"]),
        T=list(obs["T"]),
        sky=list(obs["Sky"]),
        sol=list(obs["Sol"]),
        vind=list(obs["Vind"]),
        sigt=list(obs["Sigt"]),
        kilde=list(obs["Kilde"]),
        type=list(obs["Type"]),
        uuid=list(obs["uuid"]),
    )


def arb_til_dataklasse(arb: DataFrame):
    return Arbejdssæt(
        punkt=list(arb["Punkt"]),
        fasthold=list(arb["Fasthold"]),
        hvornår=list(arb["Hvornår"]),
        kote=list(arb["Kote"]),
        sigma=list(arb["σ"]),
        ny_kote=list(arb["Ny kote"]),
        ny_sigma=list(arb["Ny σ"]),
        Delta_kote=list(arb["Δ-kote [mm]"]),
        opløft=list(arb["Opløft [mm/år]"]),
        system=list(arb["System"]),
        nord=list(arb["Nord"]),
        øst=list(arb["Øst"]),
        uuid=list(arb["uuid"]),
        udelad=list(arb["Udelad publikation"]),
    )


# ------------------------------------------------------------------------------
def spredning(
    observationstype: str,
    afstand_i_m: float,
    antal_opstillinger: float,
    afstandsafhængig_spredning_i_mm: float,
    centreringsspredning_i_mm: float,
) -> float:
    """Apriorispredning for nivellementsobservation

    Fx.  MTL: spredning("mtl", 500, 3, 2, 0.5) = 1.25
         MGL: spredning("MGL", 500, 3, 0.6, 0.01) = 0.4243
         NUL: spredning("NUL", .....) = 0

    Rejser ValueError ved ukendt observationstype eller
    (via math.sqrt) ved negativ afstand_i_m.

    Negative afstandsafhængig- eller centreringsspredninger
    behandles som positive.

    Observationstypen NUL benyttes til at sammenbinde disjunkte
    undernet - det er en observation med forsvindende apriorifejl,
    der eksakt reproducerer koteforskellen mellem to fastholdte
    punkter
    """

    if "NUL" == observationstype.upper():
        return 0

    opstillingsafhængig = sqrt(antal_opstillinger * (centreringsspredning_i_mm**2))

    if "MTL" == observationstype.upper():
        afstandsafhængig = afstandsafhængig_spredning_i_mm * afstand_i_m / 1000
        return hypot(afstandsafhængig, opstillingsafhængig)

    if "MGL" == observationstype.upper():
        afstandsafhængig = afstandsafhængig_spredning_i_mm * sqrt(afstand_i_m / 1000)
        return hypot(afstandsafhængig, opstillingsafhængig)

    raise ValueError(f"Ukendt observationstype: {observationstype}")


# ------------------------------------------------------------------------------
def find_fastholdte(arbejdssæt: Arbejdssæt, kontrol: bool) -> Dict[str, float]:
    """Find fastholdte punkter til gama beregning"""

    if kontrol:
        relevante = arbejdssæt.fasthold == "x"
    else:
        relevante = arbejdssæt.fasthold != ""

    fastholdte_punkter = tuple([arbejdssæt.punkt[relevante]])
    fastholdte_koter = tuple([arbejdssæt.kote[relevante]])
    return dict(zip(fastholdte_punkter, fastholdte_koter))


def skriv_gama_inputfil(
    projektnavn: str,
    fastholdte: dict,
    estimerede_punkter: Tuple[str, ...],
    observationer: Observationer,
):
    """
    Skriv gama-inputfil i XML-format
    """
    with open(f"{projektnavn}.xml", "wt") as gamafil:
        # Preambel
        gamafil.write(
            f"<?xml version='1.0' ?><gama-local>\n"
            f"<network angles='left-handed' axes-xy='en' epoch='0.0'>\n"
            f"<parameters\n"
            f"    algorithm='gso' angles='400' conf-pr='0.95'\n"
            f"    cov-band='0' ellipsoid='grs80' latitude='55.7' sigma-act='aposteriori'\n"
            f"    sigma-apr='1.0' tol-abs='1000.0'\n"
            f"/>\n\n"
            f"<description>\n"
            f"    Nivellementsprojekt {ascii(projektnavn)}\n"  # Gama kaster op over Windows-1252 tegn > 127
            f"</description>\n"
            f"<points-observations>\n\n"
        )

        # Fastholdte punkter
        gamafil.write("\n\n<!-- Fixed -->\n\n")
        for punkt, kote in fastholdte.items():
            gamafil.write(f"<point fix='Z' id='{punkt}' z='{kote}'/>\n")

        # Punkter til udjævning
        gamafil.write("\n\n<!-- Adjusted -->\n\n")
        for punkt in estimerede_punkter:
            gamafil.write(f"<point adj='z' id='{punkt}'/>\n")

        # Observationer
        gamafil.write("<height-differences>\n")
        for (sluk, fra, til, delta_H, L, type, opst, sigma, delta, journal) in zip(
            observationer.sluk,
            observationer.fra,
            observationer.til,
            observationer.delta_H,
            observationer.L,
            observationer.type,
            observationer.opst,
            observationer.sigma,
            observationer.delta,
            observationer.journal,
        ):
            if sluk == "x":
                continue
            gamafil.write(
                f"<dh from='{fra}' to='{til}' "
                f"val='{delta_H:+.6f}' "
                f"dist='{L:.5f}' stdev='{spredning(type, L, opst, sigma, delta):.5f}' "
                f"extern='{journal}'/>\n"
            )

        # Postambel
        gamafil.write(
            "</height-differences>\n"
            "</points-observations>\n"
            "</network>\n"
            "</gama-local>\n"
        )


def gama_udjævn(projektnavn: str, kontrol: bool):
    # Lad GNU Gama om at køre udjævningen
    if kontrol:
        beregningstype = "kontrol"
    else:
        beregningstype = "endelig"

    htmlrapportnavn = f"{projektnavn}-resultat-{beregningstype}.html"
    ret = subprocess.run(
        [
            "gama-local",
            f"{projektnavn}.xml",
            "--xml",
            f"{projektnavn}-resultat.xml",
            "--html",
            htmlrapportnavn,
        ]
    )
    if ret.returncode:
        if not Path(f"{projektnavn}-resultat.xml").is_file():
            fire.cli.print(
                "FEJL: Beregning ikke gennemført. Kontroller om nettet er sammenhængende, og ved flere net om der mangler fastholdte punkter.",
                bg="red",
                fg="white",
            )
            raise SystemExit(1)

        fire.cli.print(
            f"Check {projektnavn}-resultat-{beregningstype}.html", bg="red", fg="white"
        )
    return htmlrapportnavn


def læs_gama_output(
    projektnavn: str,
) -> Tuple[List[str], List[float], List[float], Timestamp]:
    """
    Læser output fra GNU Gama og returnerer relevante parametre til at skrive xlsx fil
    """
    with open(f"{projektnavn}-resultat.xml") as resultat:
        doc = xmltodict.parse(resultat.read())

    # Sammenhængen mellem rækkefølgen af elementer i Gamas punktliste (koteliste
    # herunder) og varianserne i covariansmatricens diagonal er uklart beskrevet:
    # I Gamas xml-resultatfil antydes at der skal foretages en ombytning.
    # Men rækkefølgen anvendt her passer sammen med det Gama præsenterer i
    # html-rapportudgaven af beregningsresultatet.
    koteliste = doc["gama-local-adjustment"]["coordinates"]["adjusted"]["point"]
    varliste = doc["gama-local-adjustment"]["coordinates"]["cov-mat"]["flt"]
    # try:
    punkter = [punkt["id"] for punkt in koteliste]
    koter = [float(punkt["z"]) for punkt in koteliste]
    varianser = [float(var) for var in varliste]
    assert len(koter) == len(varianser), "Mismatch mellem antal koter og varianser"
    tg = gyldighedstidspunkt(projektnavn)
    return (punkter, koter, varianser, tg)


# ------------------------------------------------------------------------------
def opdater_arbejdssæt(
    punkter: List[str],
    koter: List[float],
    varianser: List[float],
    arbejdssæt: Arbejdssæt,
    tg: Timestamp,
) -> Arbejdssæt:

    for j, (punkt, ny_kote, var) in enumerate(zip(punkter, koter, varianser)):
        if punkt in arbejdssæt.punkt:
            # Hvis punkt findes, sæt indeks til hvor det findes
            i = arbejdssæt.punkt.index(punkt)
            if i > j:
                # Gem info i det punkt hvis allerede skrevet
                arbejdssæt.punkt.append(arbejdssæt.punkt[i])
                arbejdssæt.ny_sigma.append(arbejdssæt.ny_sigma[i])
                arbejdssæt.hvornår.append(arbejdssæt.hvornår[i])
                arbejdssæt.system.append("DVR90")
            # Overskriv info i punkt der findes
            arbejdssæt.punkt[i] = punkt
            arbejdssæt.ny_kote[i] = ny_kote
            arbejdssæt.ny_sigma[i] = sqrt(var)

            # Ændring i millimeter...
            Delta = (ny_kote - arbejdssæt.kote[i]) * 1000.0
            # ...men vi ignorerer ændringer under mikrometerniveau
            if abs(Delta) < 0.001:
                Delta = 0
            arbejdssæt.Delta_kote[i] = Delta
            dt = tg - arbejdssæt.hvornår[i]
            dt = dt.total_seconds() / (365.25 * 86400)
            # t = 0 forekommer ved genberegning af allerede registrerede koter
            if dt == 0:
                continue
            arbejdssæt.opløft[i] = Delta / dt
            arbejdssæt.hvornår[i] = tg
            arbejdssæt.system[i] = "DVR90"
        else:
            # Tilføj nye punkter
            arbejdssæt.punkt.append(punkt)
            arbejdssæt.ny_sigma.append(sqrt(var))
            arbejdssæt.hvornår.append(tg)
            arbejdssæt.ny_kote.append(ny_kote)
            arbejdssæt.system.append("DVR90")

            # Fyld
            arbejdssæt.fasthold.append("")
            arbejdssæt.kote.append(None)
            arbejdssæt.sigma.append(None)
            arbejdssæt.Delta_kote.append(None)
            arbejdssæt.opløft.append(None)
            arbejdssæt.øst.append(None)
            arbejdssæt.nord.append(None)
            arbejdssæt.uuid.append(None)
            arbejdssæt.udelad.append("")
    return arbejdssæt
