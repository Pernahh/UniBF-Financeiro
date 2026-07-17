import os
import json
import glob
from flask import Flask, render_template, jsonify, Response, request
from flask_cors import CORS
from google.cloud import bigquery
from google.oauth2 import service_account

app = Flask(__name__)
CORS(app, origins='*')

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    return response

# 1. CREDENCIAIS: variável de ambiente (Render) > secret file (Render) > varredura local (dev)
try:
    diretorio_atual = os.path.dirname(os.path.abspath(__file__))

    if os.environ.get("GOOGLE_CREDENTIALS_JSON"):
        info_credenciais = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    else:
        arquivos_encontrados = (
            glob.glob("/etc/secrets/*cred*.json*")
            or glob.glob(os.path.join(diretorio_atual, "*cred*.json*"))
            or glob.glob(os.path.join(os.path.dirname(diretorio_atual), "*cred*.json*"))
        )
        caminho_final = arquivos_encontrados[0]
        with open(caminho_final, "r", encoding="utf-8") as f:
            info_credenciais = json.load(f)

    credentials = service_account.Credentials.from_service_account_info(info_credenciais)
    client = bigquery.Client(credentials=credentials, project=info_credenciais["project_id"])
except Exception as e:
    raise e

TABLE = "`unibf-analise-de-dados.financeiro.dre_lancamentos`"

def run_query(query, row_to_dict, query_params=None):
    """Executa query no BigQuery e retorna Response JSON sem cache em memória."""
    job_config = bigquery.QueryJobConfig(query_parameters=query_params or [])
    rows = list(client.query(query, job_config=job_config).result())
    partes = ['{"status":"sucesso","dados":[']
    primeiro = True
    for linha in rows:
        try:
            s = json.dumps(row_to_dict(linha), ensure_ascii=True)
        except Exception:
            continue
        if not primeiro:
            partes.append(',')
        partes.append(s)
        primeiro = False
    partes.append(']}')
    body = ''.join(partes).encode('utf-8')
    resp = Response(body, mimetype='application/json')
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/ultima-atualizacao')
def ultima_atualizacao():
    query = """
        SELECT FORMAT_TIMESTAMP('%d/%m/%Y %H:%M', TIMESTAMP_MILLIS(last_modified_time), 'America/Sao_Paulo') AS data
        FROM `unibf-analise-de-dados.financeiro.__TABLES__`
        WHERE table_id = 'dre_lancamentos'
    """
    try:
        rows = list(client.query(query).result())
        data = rows[0].data if rows else "—"
    except Exception:
        data = "—"
    return jsonify({"data": data})

@app.route('/api/dre-resumo')
def dre_resumo():
    """Agrega por ano + mês + classificação + tipo + empresa + centro de custo + pago.
    Payload único usado pelas abas Visão Geral, Por Categoria e Por Empresa (agregação
    adicional é feita no front, igual ao padrão do painel de captação)."""
    query = f"""
        SELECT
          Ano                                                 AS ano,
          SAFE_CAST(NULLIF(`Mes Pagamento`, '-') AS INT64)     AS mes,
          Classificacao                                        AS classificacao,
          Tipo                                                 AS tipo,
          COALESCE(`Plano de Contas`, 'Não Informado')         AS plano_contas,
          COALESCE(Empresa, 'Não Informado')                   AS empresa,
          COALESCE(`Centro de Custos`, 'Não Informado')        AS centro_custo,
          COALESCE(Pago, 'Não Informado')                      AS pago,
          SUM(`Valor Ajustado`)                                AS valor,
          COUNT(*)                                             AS qtd
        FROM {TABLE}
        GROUP BY 1,2,3,4,5,6,7,8
        ORDER BY ano, mes
    """

    def row_to_dict(l):
        return {
            "ano": int(l.ano) if l.ano is not None else None,
            "mes": int(l.mes) if l.mes is not None else None,
            "classificacao": str(l.classificacao) if l.classificacao else None,
            "tipo": str(l.tipo) if l.tipo else None,
            "plano_contas": str(l.plano_contas) if l.plano_contas else None,
            "empresa": str(l.empresa) if l.empresa else None,
            "centro_custo": str(l.centro_custo) if l.centro_custo else None,
            "pago": str(l.pago) if l.pago else None,
            "valor": float(l.valor) if l.valor is not None else 0.0,
            "qtd": int(l.qtd),
        }

    try:
        return run_query(query, row_to_dict)
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500

@app.route('/api/dre-detalhado')
def dre_detalhado():
    """Lançamentos individuais respeitando filtros — tabela e download CSV da aba Detalhamento."""
    ano = request.args.get('ano', type=int)
    mes = request.args.get('mes', type=int)
    empresa = request.args.get('empresa', default='', type=str)
    classificacao = request.args.get('classificacao', default='', type=str)
    pago = request.args.get('pago', default='', type=str)

    filtros = []
    query_params = []
    if ano:
        filtros.append("Ano = @ano")
        query_params.append(bigquery.ScalarQueryParameter('ano', 'INT64', ano))
    if mes:
        filtros.append("SAFE_CAST(NULLIF(`Mes Pagamento`, '-') AS INT64) = @mes")
        query_params.append(bigquery.ScalarQueryParameter('mes', 'INT64', mes))
    if empresa:
        filtros.append("COALESCE(Empresa, 'Não Informado') = @empresa")
        query_params.append(bigquery.ScalarQueryParameter('empresa', 'STRING', empresa))
    if classificacao:
        filtros.append("Classificacao = @classificacao")
        query_params.append(bigquery.ScalarQueryParameter('classificacao', 'STRING', classificacao))
    if pago:
        filtros.append("COALESCE(Pago, 'Não Informado') = @pago")
        query_params.append(bigquery.ScalarQueryParameter('pago', 'STRING', pago))

    where = f"WHERE {' AND '.join(filtros)}" if filtros else ""

    query = f"""
        SELECT
          Ano                                            AS ano,
          `Data Lancamento`                               AS data_lancamento,
          Classificacao                                   AS classificacao,
          Tipo                                            AS tipo,
          `Plano de Contas`                               AS plano_contas,
          COALESCE(Empresa, 'Não Informado')              AS empresa,
          `Forma de Pagamento`                            AS forma_pagamento,
          `Centro de Custos`                              AS centro_custo,
          Beneficiario                                    AS beneficiario,
          `Valor Ajustado`                                AS valor_ajustado,
          `Data Pagamento`                                AS data_pagamento,
          COALESCE(Pago, 'Não Informado')                 AS pago
        FROM {TABLE}
        {where}
        ORDER BY `Data Lancamento` DESC
    """

    def row_to_dict(l):
        return {
            "ano": int(l.ano) if l.ano is not None else None,
            "data_lancamento": l.data_lancamento.strftime('%d/%m/%Y') if l.data_lancamento else '',
            "classificacao": str(l.classificacao) if l.classificacao else '',
            "tipo": str(l.tipo) if l.tipo else '',
            "plano_contas": str(l.plano_contas) if l.plano_contas else '',
            "empresa": str(l.empresa) if l.empresa else '',
            "forma_pagamento": str(l.forma_pagamento) if l.forma_pagamento else '',
            "centro_custo": str(l.centro_custo) if l.centro_custo else '',
            "beneficiario": str(l.beneficiario) if l.beneficiario else '',
            "valor_ajustado": f"{float(l.valor_ajustado):.2f}".replace('.', ',') if l.valor_ajustado is not None else '',
            "data_pagamento": l.data_pagamento.strftime('%d/%m/%Y') if l.data_pagamento else '',
            "pago": str(l.pago) if l.pago else '',
        }

    try:
        return run_query(query, row_to_dict, query_params)
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True, threaded=True)
