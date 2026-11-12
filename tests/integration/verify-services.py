import json
import os
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


USER_ID = "m6-integration-user-1"
SECOND_USER_ID = "m6-integration-user-2"
CONTRACT_USER_ID = "m6-integration-contract-user"
SOLUTION = [
    {"characterId": "V1", "x": 0, "y": 0},
    {"characterId": "S1", "x": 1, "y": 0},
    {"characterId": "S2", "x": 0, "y": 1},
    {"characterId": "S3", "x": 2, "y": 1},
]


def call(base_url, path, method="GET", headers=None, payload=None):
    request_headers = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(
        f"{base_url}{path}",
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=15) as response:
            body = response.read()
            status = response.status
    except HTTPError as error:
        body = error.read()
        status = error.code
    except URLError as error:
        raise AssertionError(f"El servicio no respondió: {error.reason}") from error
    return status, json.loads(body) if body else {}


def expect_status(actual, expected, operation, response=None):
    if actual != expected:
        message = f"{operation}: HTTP {actual}, se esperaba {expected}"
        if response is not None:
            message += f"; respuesta: {response!r}"
        raise AssertionError(message)


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: recibido {actual!r}, esperado {expected!r}")


def partida_headers(user_id=USER_ID):
    return {
        "X-Gateway-Secret": os.environ["M6_GATEWAY_SECRET"],
        "X-User-Id": user_id,
    }


def stats_for(user_id=USER_ID):
    status, body = call(
        os.environ["M6_USERS_URL"],
        "/users/me/stats",
        headers={
            "X-Gateway-Secret": os.environ["M6_GATEWAY_SECRET"],
            "X-User-Id": user_id,
            "X-Roles": "USER",
        },
    )
    expect_status(status, 200, "Consultar estadísticas")
    return body


def create_classic(case_id):
    status, body = call(
        os.environ["M6_PARTIDAS_URL"],
        "/matches/classic",
        method="POST",
        headers=partida_headers(),
        payload={"caseId": case_id},
    )
    expect_status(status, 200, "Crear partida CLASSIC")
    assert_equal(body["caseId"], case_id, "El caso creado no coincide")
    assert_equal(body["status"], "IN_PROGRESS", "Estado inicial")
    return body


def submit_classic(match_id, attempt_id, positions):
    return call(
        os.environ["M6_PARTIDAS_URL"],
        f"/matches/classic/{match_id}/attempts",
        method="POST",
        headers=partida_headers(),
        payload={"attemptId": attempt_id, "positions": positions},
    )


def classic_attempt(case_id, positions):
    match = create_classic(case_id)
    attempt_id = str(uuid.uuid4())
    return match, attempt_id, submit_classic(match["matchId"], attempt_id, positions)


def verify_case_service():
    status, cases = call(
        os.environ["M6_CASES_URL"],
        "/cases",
        headers={"X-Gateway-Secret": os.environ["M6_GATEWAY_SECRET"]},
    )
    expect_status(status, 200, "Listar casos")
    case = next(
        (item for item in cases if item.get("title") == "Muerte en la cocina"),
        None,
    )
    if not case:
        raise AssertionError("El caso semilla de integración no está disponible")
    assert_equal(case["difficulty"], "EASY", "Dificultad del caso semilla")
    return case["id"]


def verify_users_internal_endpoint():
    payload = {
        "matchId": str(uuid.uuid4()),
        "caseId": "m6-integration-contract-case",
        "mode": "CLASSIC",
        "won": True,
        "timeSeconds": 1,
        "difficulty": "EASY",
        "xpDelta": None,
    }
    status, response = call(
        os.environ["M6_USERS_URL"],
        f"/users/{CONTRACT_USER_ID}/results",
        method="POST",
        headers={"X-Internal-Token": os.environ["M6_INTERNAL_TOKEN"]},
        payload=payload,
    )
    expect_status(status, 200, "Contrato HTTP interno de users-service", response)
    assert_equal(response["xpDelta"], 50, "XP calculada por users-service")
    assert_equal(response["firstWinRewarded"], True, "Recompensa de B3")
    print("USERS: contrato B3 HTTP verificado directamente")


def verify_classic(case_id):
    incorrect = [dict(position) for position in SOLUTION]
    incorrect[0]["x"] = 2
    match = create_classic(case_id)
    attempt_id = str(uuid.uuid4())

    status, first = submit_classic(match["matchId"], attempt_id, incorrect)
    expect_status(status, 200, "Intento incorrecto")
    assert_equal(first["valid"], False, "El intento incorrecto debe rechazarse")
    assert_equal(first["status"], "IN_PROGRESS", "La partida debe seguir abierta")

    status, replay = submit_classic(match["matchId"], attempt_id, incorrect)
    expect_status(status, 200, "Reintento idéntico")
    assert_equal(replay, first, "El reintento debe devolver el mismo resultado")

    contradictory = [dict(position) for position in incorrect]
    contradictory[0]["y"] = 1
    status, _ = submit_classic(match["matchId"], attempt_id, contradictory)
    expect_status(status, 409, "Reutilizar attemptId con contenido distinto")
    assert_equal(stats_for()["experience"], 0, "El intento incorrecto no debe dar XP")

    winning_match, winning_attempt, (status, win) = classic_attempt(case_id, SOLUTION)
    expect_status(status, 200, "Solución CLASSIC correcta", win)
    assert_equal(win["valid"], True, "La solución correcta debe aceptarse")
    assert_equal(win["status"], "FINISHED", "La partida ganada debe finalizar")
    assert_equal(win["xpDelta"], 50, "La primera victoria EASY debe dar 50 XP")
    assert_equal(win["firstWinRewarded"], True, "Debe marcar la recompensa inicial")

    status, replayed_win = submit_classic(
        winning_match["matchId"],
        winning_attempt,
        SOLUTION,
    )
    expect_status(status, 200, "Reintento de victoria")
    assert_equal(replayed_win, win, "El reintento de victoria debe ser idéntico")

    current_stats = stats_for()
    assert_equal(current_stats["experience"], 50, "XP tras primera victoria")
    assert_equal(current_stats["solvedCases"], 1, "Victorias registradas")
    assert_equal(current_stats["uniqueCasesSolved"], 1, "Casos únicos resueltos")

    repeated_result = {
        "matchId": winning_match["matchId"],
        "caseId": case_id,
        "mode": "CLASSIC",
        "won": True,
        "timeSeconds": current_stats["totalTime"],
        "difficulty": "EASY",
        "xpDelta": None,
    }
    status, user_replay = call(
        os.environ["M6_USERS_URL"],
        f"/users/{USER_ID}/results",
        method="POST",
        headers={"X-Internal-Token": os.environ["M6_INTERNAL_TOKEN"]},
        payload=repeated_result,
    )
    expect_status(status, 200, "Repetir resultado idéntico en users-service")
    assert_equal(user_replay["xpDelta"], 50, "XP persistida en users-service")
    assert_equal(user_replay["firstWinRewarded"], True, "Recompensa idempotente")

    contradictory_result = dict(repeated_result)
    contradictory_result["timeSeconds"] += 1
    status, _ = call(
        os.environ["M6_USERS_URL"],
        f"/users/{USER_ID}/results",
        method="POST",
        headers={"X-Internal-Token": os.environ["M6_INTERNAL_TOKEN"]},
        payload=contradictory_result,
    )
    expect_status(status, 409, "Conflicto por resultado terminal contradictorio")

    _, _, (status, later_win) = classic_attempt(case_id, SOLUTION)
    expect_status(status, 200, "Segunda victoria CLASSIC del mismo caso")
    assert_equal(later_win["xpDelta"], 0, "La victoria repetida no debe dar XP")
    assert_equal(
        later_win["firstWinRewarded"],
        False,
        "No debe reclamar dos veces la recompensa del caso",
    )

    final_stats = stats_for()
    assert_equal(final_stats["experience"], 50, "La XP CLASSIC solo se concede una vez")
    assert_equal(final_stats["solvedCases"], 2, "Se conservan ambas victorias")
    assert_equal(final_stats["uniqueCasesSolved"], 1, "El caso se cuenta una sola vez")
    print("CLASSIC: intentos, 409, replay, XP única y uniqueCasesSolved verificados")
    return final_stats["experience"]


def verify_timed(case_id, previous_experience):
    status, match = call(
        os.environ["M6_PARTIDAS_URL"],
        "/matches/timed",
        method="POST",
        headers=partida_headers(),
        payload={"caseId": case_id, "timeLimitSeconds": 3600},
    )
    expect_status(status, 200, "Crear partida TIMED")

    status, result = call(
        os.environ["M6_PARTIDAS_URL"],
        f"/matches/timed/{match['matchId']}/finish",
        method="POST",
        headers=partida_headers(),
        payload={"positions": SOLUTION},
    )
    expect_status(status, 200, "Finalizar partida TIMED")
    assert_equal(result["won"], True, "La solución TIMED debe ganar")

    current_stats = stats_for()
    assert_equal(
        current_stats["experience"],
        previous_experience + result["xpDelta"],
        "TIMED debe conservar el XP que calcula partidas-service",
    )
    print("TIMED: contrato de resultado y XP calculada por el backend verificados")
    return current_stats["experience"]


def verify_versus(case_id, first_user_experience):
    status, match = call(
        os.environ["M6_PARTIDAS_URL"],
        "/matches/versus",
        method="POST",
        headers=partida_headers(),
        payload={"caseId": case_id},
    )
    expect_status(status, 200, "Crear partida VERSUS")

    status, joined = call(
        os.environ["M6_PARTIDAS_URL"],
        f"/matches/versus/{match['matchId']}/join",
        method="POST",
        headers=partida_headers(SECOND_USER_ID),
    )
    expect_status(status, 200, "Unirse a partida VERSUS")
    assert_equal(joined["status"], "IN_PROGRESS", "Estado VERSUS tras unirse")

    status, result = call(
        os.environ["M6_PARTIDAS_URL"],
        f"/matches/versus/{match['matchId']}/submit",
        method="POST",
        headers=partida_headers(SECOND_USER_ID),
        payload={"positions": SOLUTION},
    )
    expect_status(status, 200, "Enviar solución VERSUS")
    assert_equal(result["winnerId"], SECOND_USER_ID, "Ganador VERSUS")
    assert_equal(result["xpDelta"], 100, "XP VERSUS")

    first_stats = stats_for(USER_ID)
    second_stats = stats_for(SECOND_USER_ID)
    assert_equal(
        first_stats["experience"],
        first_user_experience,
        "La derrota VERSUS no debe cambiar la XP del rival",
    )
    assert_equal(second_stats["experience"], 100, "XP del ganador VERSUS")
    assert_equal(second_stats["solvedCases"], 1, "Victoria VERSUS registrada")
    print("VERSUS: ambos resultados y la XP del ganador verificados")


def main():
    case_id = verify_case_service()
    verify_users_internal_endpoint()
    classic_experience = verify_classic(case_id)
    timed_experience = verify_timed(case_id, classic_experience)
    verify_versus(case_id, timed_experience)
    print("Integración M6 aislada: todas las comprobaciones pasaron")


if __name__ == "__main__":
    main()
