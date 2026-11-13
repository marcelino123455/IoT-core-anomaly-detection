"""Cliente de inferencia sobre mediciones remotas de AWS.

GET /measurements -> {"samples": [{"id":"...", "temperatura_C": ..., ...,
                                      "estado_ventilador":"ENCENDIDO"}]}
POST /anomalies  <- evento ANOMALY_SHUTDOWN_REQUEST. La infraestructura
AWS debe implementar el control real del relé de forma autenticada.

Sin ENABLE_REMOTE_SHUTDOWN=true: SOLO LOGS, nunca envía solicitudes OFF.
"""
from __future__ import annotations
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parent
load_dotenv(ROOT/'.env')
GET_URL=os.getenv('AWS_GET_URL','').strip()
POST_URL=os.getenv('AWS_POST_URL','').strip()
API_KEY=os.getenv('AWS_API_KEY','').strip()
POLL_SECONDS=float(os.getenv('POLL_SECONDS','2'))
REQUIRED=max(2,int(os.getenv('CONSECUTIVE_ANOMALIES','3')))
ARMED=os.getenv('ENABLE_REMOTE_SHUTDOWN','false').lower()=='true'
LOG=ROOT/'results'/'aws_inference.jsonl'
STATE=ROOT/'results'/'aws_client_state.json'
logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s')


def model_predict(sample,artifact):
    features=artifact['features']
    try:
        vals=[float(sample[c]) for c in features]
    except (KeyError,TypeError,ValueError) as e:
        raise ValueError('Faltan features o son invalidas') from e
    if not np.all(np.isfinite(vals)):
        raise ValueError('Valores no finitos')
    x=pd.DataFrame([vals],columns=features)
    score=float(-artifact['pipeline'].score_samples(x)[0])
    cal=artifact['calibration_scores']
    # Percentil del score entre registros de calibracion normales;
    # NO equivale a probabilidad calibrada de averia.
    rarity=100*float(np.searchsorted(cal,score,side='right')/len(cal))
    return {'is_anomaly':bool(score>artifact['threshold']),
            'score':round(score,6),'threshold':round(float(artifact['threshold']),6),
            'normal_reference_percentile':round(rarity,2),
            'model_version':artifact['version']}


def get_samples(session):
    r=session.get(GET_URL,timeout=12)
    r.raise_for_status(); payload=r.json()
    if isinstance(payload,list): return payload
    if isinstance(payload,dict):
        for key in ('samples','measurements','data'):
            if isinstance(payload.get(key),list): return payload[key]
    raise ValueError('Respuesta GET: se requiere lista o {"samples":[...]}')


def main():
    if not GET_URL.startswith('https://') or not POST_URL.startswith('https://'):
        raise ValueError('Configura AWS_GET_URL y AWS_POST_URL HTTPS validos en .env')
    if POLL_SECONDS<=0: raise ValueError('POLL_SECONDS debe ser positivo')
    artifact=joblib.load(ROOT/'models'/'best_model.joblib')
    session=requests.Session()
    session.headers.update({'Accept':'application/json','Content-Type':'application/json'})
    if API_KEY: session.headers.update({'x-api-key':API_KEY})
    ROOT.joinpath('results').mkdir(exist_ok=True)
    try: state=json.loads(STATE.read_text(encoding='utf-8'))
    except (FileNotFoundError,json.JSONDecodeError): state={'seen_ids':[],'shutdown_pending':False}
    seen=set(str(x) for x in state.get('seen_ids',[]))
    recent=deque(maxlen=REQUIRED)
    shutdown_pending=bool(state.get('shutdown_pending',False))
    logging.warning('APAGADO REMOTO HABILITADO=%s (false es modo seguro)',ARMED)
    logging.info('Se requieren %d anomalias consecutivas de un mismo estado ENCENDIDO',REQUIRED)
    while True:
        try:
            samples=get_samples(session)
            # Requisito contrato: GET devuelve mediciones con ID persistente y orden temporal.
            for sample in samples:
                if not isinstance(sample,dict): continue
                uid=sample.get('id')
                if uid is None:
                    logging.warning('Omitida medicion sin id de evento');continue
                uid=str(uid)
                if uid in seen:continue
                seen.add(uid)
                status=str(sample.get('estado_ventilador','')).strip().upper()
                if status!='ENCENDIDO':
                    recent.clear()
                    if status=='APAGADO': shutdown_pending=False
                    decision={'is_anomaly':None,'note':'Estado no ENCENDIDO: omitida inferencia'}
                else:
                    try:
                        decision=model_predict(sample,artifact)
                        recent.append(decision['is_anomaly'])
                    except ValueError as e:
                        recent.clear();decision={'is_anomaly':None,'error':str(e)}
                event={'sample_id':uid,'timestamp_utc':sample.get('fecha_hora'),
                       'state_reported':status,'decision':decision}
                with LOG.open('a',encoding='utf-8') as log:
                    log.write(json.dumps(event,ensure_ascii=False)+'\n')
                logging.info('%s state=%s result=%s',uid,status,decision)
                sustained=(len(recent)==REQUIRED and all(recent))
                if sustained and not shutdown_pending:
                    command={'event_type':'ANOMALY_SHUTDOWN_REQUEST',
                             'sample_id':uid,'command':'OFF',
                             'idempotency_key':f'ventiset-off-{uid}',
                             'model_result':decision}
                    if not ARMED:
                        logging.warning('SIMULACION: se solicitaría OFF; no se envia a AWS')
                    else:
                        # AWS debe autenticar, validar el evento, y enviar la orden real.
                        resp=session.post(POST_URL,json=command,timeout=12)
                        resp.raise_for_status()
                        shutdown_pending=True
                        logging.warning('Solicitud OFF aceptada por API (no confirma rele fisico)')
                state={'seen_ids':list(seen)[-3000:],'shutdown_pending':shutdown_pending}
                STATE.write_text(json.dumps(state,ensure_ascii=False),encoding='utf-8')
        except (requests.RequestException,ValueError,KeyError,OSError) as exc:
            logging.error('AWS/entrada: %s',exc)
        time.sleep(POLL_SECONDS)

if __name__=='__main__':
    try: main()
    except KeyboardInterrupt: print('\nCliente detenido')
