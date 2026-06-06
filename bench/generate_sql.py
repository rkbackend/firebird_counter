from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True)
class ClientScriptSpec:
    client_id: int
    path: str
    rounds: int
    execute_procedure_calls: int
    logical_operations: int


@dataclass(slots=True)
class GeneratedBenchmark:
    bootstrap_sql: str
    reset_sql: str
    client_scripts: list[ClientScriptSpec]
    procedure_count: int
    client_count: int
    rounds_per_client: int
    expected_execute_procedure_calls: int
    expected_logical_operations: int
    data_row_count: int
    aux_row_count: int


def procedure_name(index: int) -> str:
    return f"BENCH_PROC_{index:04d}"


def _procedure_key(index: int, data_row_count: int) -> int:
    return ((index - 1) % data_row_count) + 1


def _render_bootstrap_sql(database_path: str, procedure_count: int, data_row_count: int, aux_row_count: int) -> str:
    lines: list[str] = [
        f"CREATE DATABASE '{database_path}';",
        "COMMIT;",
        "",
        "CREATE TABLE BENCH_DATA (",
        "    ID INTEGER NOT NULL PRIMARY KEY,",
        "    CALL_COUNT INTEGER NOT NULL,",
        "    PAYLOAD INTEGER NOT NULL,",
        "    UPDATED_AT TIMESTAMP NOT NULL",
        ");",
        "",
        "CREATE TABLE BENCH_AUX (",
        "    ID INTEGER NOT NULL PRIMARY KEY,",
        "    VALUE_INTEGER INTEGER NOT NULL,",
        "    UPDATED_AT TIMESTAMP NOT NULL",
        ");",
        "",
        "CREATE TABLE BENCH_TRANSIENT (",
        "    ID BIGINT NOT NULL PRIMARY KEY,",
        "    CLIENT_ID INTEGER NOT NULL,",
        "    ROUND_NO INTEGER NOT NULL,",
        "    PAYLOAD INTEGER NOT NULL,",
        "    CREATED_AT TIMESTAMP NOT NULL",
        ");",
        "",
        "CREATE TABLE BENCH_LOG (",
        "    LOG_ID BIGINT NOT NULL PRIMARY KEY,",
        "    PROC_NAME VARCHAR(63) NOT NULL,",
        "    DATA_ID INTEGER NOT NULL,",
        "    EVENT_TS TIMESTAMP NOT NULL,",
        "    NOTE VARCHAR(32) NOT NULL",
        ");",
        "",
        "CREATE SEQUENCE SEQ_BENCH_LOG;",
        "COMMIT;",
        "",
        "SET TERM ^;",
        "EXECUTE BLOCK AS",
        "  DECLARE I INTEGER = 1;",
        "BEGIN",
        f"  WHILE (I <= {data_row_count}) DO",
        "  BEGIN",
        "    INSERT INTO BENCH_DATA (ID, CALL_COUNT, PAYLOAD, UPDATED_AT)",
        "    VALUES (:I, 0, :I * 10, CURRENT_TIMESTAMP);",
        "    I = I + 1;",
        "  END",
        "END^",
        "",
        "EXECUTE BLOCK AS",
        "  DECLARE I INTEGER = 1;",
        "BEGIN",
        f"  WHILE (I <= {aux_row_count}) DO",
        "  BEGIN",
        "    INSERT INTO BENCH_AUX (ID, VALUE_INTEGER, UPDATED_AT)",
        "    VALUES (:I, :I * 100, CURRENT_TIMESTAMP);",
        "    I = I + 1;",
        "  END",
        "END^",
        "SET TERM ;^",
        "",
        "COMMIT;",
        "",
        "SET TERM ^;",
    ]

    for proc_index in range(1, procedure_count + 1):
        proc_name = procedure_name(proc_index)
        key_id = _procedure_key(proc_index, data_row_count)
        lines.extend(
            [
                f"CREATE OR ALTER PROCEDURE {proc_name}",
                "AS",
                "  DECLARE CURRENT_PAYLOAD INTEGER;",
                "BEGIN",
                f"  SELECT PAYLOAD FROM BENCH_DATA WHERE ID = {key_id} INTO :CURRENT_PAYLOAD;",
                "  UPDATE BENCH_DATA",
                "     SET CALL_COUNT = CALL_COUNT + 1,",
                "         PAYLOAD = :CURRENT_PAYLOAD + 1,",
                "         UPDATED_AT = CURRENT_TIMESTAMP",
                f"   WHERE ID = {key_id};",
            ]
        )

        # Every tenth procedure performs an additional INSERT so the procedure
        # workload includes a small write amplification and log contention.
        if proc_index % 10 == 0:
            lines.extend(
                [
                    "  INSERT INTO BENCH_LOG (LOG_ID, PROC_NAME, DATA_ID, EVENT_TS, NOTE)",
                    "  VALUES (NEXT VALUE FOR SEQ_BENCH_LOG, "
                    f"'{proc_name}', {key_id}, CURRENT_TIMESTAMP, 'periodic');",
                ]
            )

        lines.extend(["END^", ""])

    lines.extend(["SET TERM ;^", "", "COMMIT;"])
    return "\n".join(lines) + "\n"


def _render_reset_sql(data_row_count: int) -> str:
    return "\n".join(
        [
            "DELETE FROM BENCH_LOG;",
            "DELETE FROM BENCH_TRANSIENT;",
            "UPDATE BENCH_DATA",
            "   SET CALL_COUNT = 0,",
            "       PAYLOAD = ID * 10,",
            "       UPDATED_AT = CURRENT_TIMESTAMP;",
            "UPDATE BENCH_AUX",
            "   SET VALUE_INTEGER = ID * 100,",
            "       UPDATED_AT = CURRENT_TIMESTAMP;",
            "COMMIT;",
            "",
        ]
    )


def _build_client_script(
    client_id: int,
    client_count: int,
    rounds_per_client: int,
    procedure_count: int,
    aux_row_count: int,
) -> tuple[str, int, int]:
    lines = [
        "SET BAIL ON;",
        "SET LIST OFF;",
        "SET HEADING OFF;",
        "SET COUNT OFF;",
        "SET PLAN OFF;",
        "SET ECHO OFF;",
        "SET TERM ^;",
        "",
    ]
    execute_procedure_calls = 0
    logical_operations = 0
    procedure_slice_start = ((client_id - 1) * procedure_count) // client_count + 1
    procedure_slice_end = (client_id * procedure_count) // client_count
    if procedure_slice_end < procedure_slice_start:
        procedure_slice_start = 1
        procedure_slice_end = procedure_count
    procedure_slice_width = max(1, procedure_slice_end - procedure_slice_start + 1)

    aux_slice_start = ((client_id - 1) * aux_row_count) // client_count + 1
    aux_slice_end = (client_id * aux_row_count) // client_count
    if aux_slice_end < aux_slice_start:
        aux_slice_start = 1
        aux_slice_end = aux_row_count
    aux_slice_width = max(1, aux_slice_end - aux_slice_start + 1)

    for round_index in range(rounds_per_client):
        for op_index in range(7):
            procedure_index = procedure_slice_start + (((round_index * 17) + (op_index * 97)) % procedure_slice_width)
            lines.append(f"EXECUTE PROCEDURE {procedure_name(procedure_index)}^")
            execute_procedure_calls += 1
            logical_operations += 1

        select_key = aux_slice_start + (((round_index * 5) + client_id) % aux_slice_width)
        update_key = aux_slice_start + (((round_index * 7) + (client_id * 3)) % aux_slice_width)
        transient_id = client_id * 1_000_000 + round_index

        # Direct SELECT wrapped in EXECUTE BLOCK keeps isql quiet while still
        # exercising a normal indexed lookup outside stored procedures.
        lines.extend(
            [
                "EXECUTE BLOCK AS",
                "  DECLARE CURRENT_VALUE INTEGER;",
                "BEGIN",
                f"  SELECT VALUE_INTEGER FROM BENCH_AUX WHERE ID = {select_key} INTO :CURRENT_VALUE;",
                "END^",
                (
                    "UPDATE BENCH_AUX "
                    f"SET VALUE_INTEGER = VALUE_INTEGER + 1, UPDATED_AT = CURRENT_TIMESTAMP WHERE ID = {update_key}^"
                ),
                "EXECUTE BLOCK AS",
                "BEGIN",
                "  INSERT INTO BENCH_TRANSIENT (ID, CLIENT_ID, ROUND_NO, PAYLOAD, CREATED_AT)",
                f"  VALUES ({transient_id}, {client_id}, {round_index}, {transient_id % 1000}, CURRENT_TIMESTAMP);",
                f"  DELETE FROM BENCH_TRANSIENT WHERE ID = {transient_id};",
                "END^",
                "COMMIT^",
                "",
            ]
        )
        logical_operations += 3

    lines.append("SET TERM ;^")
    return "\n".join(lines) + "\n", execute_procedure_calls, logical_operations


def generate_benchmark_artifacts(
    output_dir: Path,
    database_path: str,
    client_count: int,
    procedure_count: int,
    rounds_per_client: int,
    data_row_count: int | None = None,
    aux_row_count: int | None = None,
) -> GeneratedBenchmark:
    output_dir.mkdir(parents=True, exist_ok=True)
    data_row_count = data_row_count or max(procedure_count, 2048)
    aux_row_count = aux_row_count or max(procedure_count, 2048)

    bootstrap_sql = output_dir / "bootstrap.sql"
    reset_sql = output_dir / "reset.sql"
    bootstrap_sql.write_text(
        _render_bootstrap_sql(
            database_path=database_path,
            procedure_count=procedure_count,
            data_row_count=data_row_count,
            aux_row_count=aux_row_count,
        ),
        encoding="utf-8",
    )
    reset_sql.write_text(_render_reset_sql(data_row_count=data_row_count), encoding="utf-8")

    client_scripts: list[ClientScriptSpec] = []
    expected_execute_procedure_calls = 0
    expected_logical_operations = 0
    for client_id in range(1, client_count + 1):
        client_path = output_dir / f"client_{client_id:03d}.sql"
        script_body, proc_calls, logical_ops = _build_client_script(
            client_id=client_id,
            client_count=client_count,
            rounds_per_client=rounds_per_client,
            procedure_count=procedure_count,
            aux_row_count=aux_row_count,
        )
        client_path.write_text(script_body, encoding="utf-8")
        client_scripts.append(
            ClientScriptSpec(
                client_id=client_id,
                path=str(client_path),
                rounds=rounds_per_client,
                execute_procedure_calls=proc_calls,
                logical_operations=logical_ops,
            )
        )
        expected_execute_procedure_calls += proc_calls
        expected_logical_operations += logical_ops

    manifest = GeneratedBenchmark(
        bootstrap_sql=str(bootstrap_sql),
        reset_sql=str(reset_sql),
        client_scripts=client_scripts,
        procedure_count=procedure_count,
        client_count=client_count,
        rounds_per_client=rounds_per_client,
        expected_execute_procedure_calls=expected_execute_procedure_calls,
        expected_logical_operations=expected_logical_operations,
        data_row_count=data_row_count,
        aux_row_count=aux_row_count,
    )
    (output_dir / "manifest.json").write_text(json.dumps(asdict(manifest), indent=2), encoding="utf-8")
    return manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate Firebird benchmark SQL artifacts.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--database-path", required=True)
    parser.add_argument("--client-count", type=int, default=64)
    parser.add_argument("--procedure-count", type=int, default=1000)
    parser.add_argument("--rounds-per-client", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    manifest = generate_benchmark_artifacts(
        output_dir=args.output_dir,
        database_path=args.database_path,
        client_count=args.client_count,
        procedure_count=args.procedure_count,
        rounds_per_client=args.rounds_per_client,
    )
    print(json.dumps(asdict(manifest), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
