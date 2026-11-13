"""VentiSet: entrenamiento exploratorio de detector de anomalías.

Uso: python main.py [--csv data/VentiSet.csv] [--seed 42]
Nota: modelo de investigacion; no usar como unico mecanismo de seguridad.
"""
from __future__ import annotations
import argparse
import json
import warnings
from pathlib import Path
import joblib
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.decomposition import PCA
from sklearn.ensemble import IsolationForest
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import RobustScaler
from itertools import product
from tqdm.auto import tqdm

BASE = Path(__file__).resolve().parent
FEATURES = ['temperatura_C','aceleracion_x','aceleracion_y','aceleracion_z','corriente_mV']
EXPECTED = ['fecha_hora','tiempo_s',*FEATURES,'cambio_mV','estado_ventilador']


def load_dataset(path: Path):
    # En el dataset recibido hay 268 registros de 8 campos y 3993 de 9;
    # la cabecera tiene solo 8. Forzamos las 9 columnas y no descartamos filas.
    df = pd.read_csv(path, header=None, names=EXPECTED, skiprows=1,
                     dtype=str, engine='python', on_bad_lines='warn')
    df = df.replace({'': np.nan, 'None': np.nan})
    for col in ['tiempo_s', *FEATURES, 'cambio_mV']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['fecha_hora'] = pd.to_datetime(df['fecha_hora'], errors='coerce')
    df['estado_ventilador'] = df['estado_ventilador'].fillna('SIN_ESTADO').str.strip().str.upper()
    original = len(df)
    # Entradas inválidas del DS18B20 y lecturas perdidas no representan fallas mecánicas.
    valid = df.dropna(subset=FEATURES).copy()
    valid = valid[valid.temperatura_C.between(-40, 100)]
    # Retener el orden de captura; no ordenar por tiempo_s porque ESP32 puede reiniciarse.
    valid = valid.reset_index(drop=True)
    on = valid.loc[valid.estado_ventilador == 'ENCENDIDO'].copy().reset_index(drop=True)
    print(f'Registros CSV: {original}; válidos: {len(valid)}; ENCENDIDO: {len(on)}')
    print('Estados:', df.estado_ventilador.value_counts().to_dict())
    if len(on) < 150:
        raise ValueError('Se requieren al menos 150 registros ENCENDIDO validos para esta demo.')
    return df, valid, on


def setup():
    for d in ('plots','models','results'):
        (BASE/d).mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({'figure.dpi':105,'savefig.dpi':170,'font.size':10})


def save(fig, name):
    fig.savefig(BASE/'plots'/name, bbox_inches='tight')
    plt.close(fig)


def plot_data(on):
    fig, axes = plt.subplots(5,1,figsize=(13,11),sharex=True)
    for ax, c in zip(axes,FEATURES):
        ax.plot(on.index, on[c],linewidth=.65)
        ax.set_ylabel(c)
        ax.grid(alpha=.2)
    axes[-1].set_xlabel('Índice de registro ENCENDIDO')
    fig.suptitle('VentiSet | señales de sensores durante ENCENDIDO')
    save(fig,'01_senales.png')

    corr=on[FEATURES].corr()
    fig,ax=plt.subplots(figsize=(8,6))
    img=ax.imshow(corr, vmin=-1,vmax=1,cmap='coolwarm')
    fig.colorbar(img,ax=ax,label='Correlación de Pearson')
    ax.set_xticks(range(len(FEATURES)),FEATURES,rotation=40,ha='right')
    ax.set_yticks(range(len(FEATURES)),FEATURES)
    for i in range(len(FEATURES)):
        for j in range(len(FEATURES)):
            if np.isfinite(corr.iloc[i,j]):
                ax.text(j,i,f'{corr.iloc[i,j]:.2f}',ha='center',va='center',fontsize=8)
    ax.set_title('Correlaciones entre sensores (datos ENCENDIDO)')
    save(fig,'02_correlaciones.png')


def main(csv_path:Path, seed:int):
    setup()
    raw, valid, on = load_dataset(csv_path)
    plot_data(on)
    n=len(on)
    i=int(.7*n); j=int(.85*n)
    train,calib,test = on.iloc[:i],on.iloc[i:j],on.iloc[j:]
    # Split cronologico: no contaminar training con calibración y test.
    # Calibracion y seleccion SIN tocar test. Dentro del bloque temporal de
    # calibracion: primera mitad para umbral, segunda mitad para validacion.
    # best = mejor candidato en validacion; last = ultimo candidato explorado.
    # La validacion sintetica es una prueba exploratoria, NO mide recall real.
    half=max(1,len(calib)//2)
    threshold_data=calib.iloc[:half][FEATURES]
    selection_data=calib.iloc[half:][FEATURES]
    if len(selection_data)<25:
        raise ValueError('Se requieren al menos 25 registros para seleccion temporal.')
    rng_selection=np.random.default_rng(seed+51)
    train_iqr=(train[FEATURES].quantile(.75)-train[FEATURES].quantile(.25)).to_numpy(dtype=float)
    train_std=train[FEATURES].std().to_numpy(dtype=float)
    scales_selection=np.where(train_iqr>1e-9,train_iqr,np.maximum(train_std,1e-6))
    # Direcciones fijas, comunes a TODOS los candidatos para comparacion justa.
    synthetic_directions=rng_selection.normal(size=(3,len(selection_data),len(FEATURES)))
    synthetic_sets=[pd.DataFrame(selection_data.to_numpy(dtype=float)
                  +factor*scales_selection*direction,columns=FEATURES)
                  for factor,direction in product((1.5,3.0),synthetic_directions)]

    trials=[]
    best_utility=-float('inf')
    best_artifact=None
    last_artifact=None

    configuraciones = list(product(
        (100, 250, 400),
        (128, 256),
        (0.8, 1.0)
    ))

    for idx, (trees, samples, feature_fraction) in enumerate(
        tqdm(configuraciones, desc="Entrenando VentiSet", unit="modelo"),
        start=1
    ):
        candidate=Pipeline([
          ('imputer',SimpleImputer(strategy='median')),
          ('scaler',RobustScaler()),
          ('detector',IsolationForest(n_estimators=trees,max_samples=samples,
                                     max_features=feature_fraction,contamination='auto',
                                     random_state=seed,n_jobs=-1))
        ])
        candidate.fit(train[FEATURES])
        cal_scores=-candidate.score_samples(threshold_data)
        candidate_threshold=float(np.quantile(cal_scores,.99))
        val_scores=-candidate.score_samples(selection_data)
        false_positive_rate=float(np.mean(val_scores>candidate_threshold))
        synthetic_tpr=float(np.mean([
            np.mean(-candidate.score_samples(synth)>candidate_threshold)
            for synth in synthetic_sets
        ]))
        # Rechazar modelos con demasiadas falsas alarmas; para los admisibles
        # maximizar sensibilidad al ruido, penalizando falsas alarmas.
        max_fpr=0.05
        utility=synthetic_tpr-2.0*false_positive_rate
        eligible=false_positive_rate<=max_fpr
        ranking=(int(eligible),utility,-false_positive_rate)
        params={'n_estimators':trees,'max_samples':samples,'max_features':feature_fraction}
        trial={'trial':idx,**params,'validation_false_positive_rate':false_positive_rate,
               'synthetic_detection_rate':synthetic_tpr,'selection_utility':utility,
               'eligible':eligible,'threshold':candidate_threshold}
        trials.append(trial)
        artifact={'pipeline':candidate,'features':FEATURES,'threshold':candidate_threshold,
                  'version':'ventiset-iforest-v2','calibration_scores':np.sort(cal_scores),
                  'noise_scales':scales_selection,'training_rows':len(train),
                  'selection_method':'time_holdout_validation_synthetic_proxy',
                  'selected_params':params,'trial':idx,'selection_metrics':trial}
        last_artifact=artifact
        joblib.dump(artifact,BASE/'models'/'last_model.joblib')
        if best_artifact is None or ranking>best_ranking:
            best_ranking=ranking
            best_artifact=artifact
            best_utility=utility
            joblib.dump(best_artifact,BASE/'models'/'best_model.joblib')
        print(f'Candidato {idx:02d}/12 | val FPR={false_positive_rate:.3f} '
              f'| ruido detectado={synthetic_tpr:.3f} | best={best_artifact["trial"]}')

    pd.DataFrame(trials).to_csv(BASE/'results'/'hyperparameter_trials.csv',index=False)
    # El modelo usado en plots, metricas e inferencia es BEST.
    pipeline=best_artifact['pipeline']
    threshold=float(best_artifact['threshold'])
    score=lambda x:-pipeline.score_samples(x[FEATURES] if isinstance(x,pd.DataFrame) else x)
    s_train=score(train);s_cal=score(calib);s_test=score(test)
    false_alarm=float(np.mean(s_test>threshold))
    # Alias de compatibilidad con versiones anteriores: MISMO best, no otro fit.
    joblib.dump(best_artifact,BASE/'models'/'ventiset_model.joblib')

    fig,ax=plt.subplots(figsize=(9,5))
    ax.hist(s_train,bins=35,density=True,alpha=.5,label='Entrenamiento')
    ax.hist(s_cal,bins=35,density=True,alpha=.5,label='Calibración')
    ax.hist(s_test,bins=35,density=True,alpha=.5,label='Prueba temporal')
    ax.axvline(threshold,linestyle='--',color='crimson',label='Umbral p99 calibración')
    ax.set(xlabel='Score de anomalía (más alto = más inusual)',ylabel='Densidad',title='Distribución de puntajes')
    ax.legend();save(fig,'03_scores.png')

    fig,ax=plt.subplots(figsize=(12,4))
    ax.plot(np.arange(len(test)),s_test,linewidth=.9,label='Score test')
    ax.axhline(threshold,color='crimson',linestyle='--',label='Umbral')
    ax.set(xlabel='Índice de prueba cronológica',ylabel='Score',title='Evolución temporal del score de anomalía')
    ax.legend();ax.grid(alpha=.2);save(fig,'04_score_temporal.png')

    scaler=pipeline.named_steps['scaler']; imp=pipeline.named_steps['imputer']; detector=pipeline.named_steps['detector']
    x_train_scaled=scaler.transform(imp.transform(train[FEATURES]))
    pca=PCA(n_components=2,random_state=seed).fit(x_train_scaled)
    z=pca.transform(x_train_scaled)
    # Corte PCA bidimensional: el resto de dimensiones se mantienen en cero (centro escalado).
    xx=np.linspace(np.percentile(z[:,0],1)-2,np.percentile(z[:,0],99)+2,125)
    yy=np.linspace(np.percentile(z[:,1],1)-2,np.percentile(z[:,1],99)+2,125)
    mesh=np.array(np.meshgrid(xx,yy));coords=mesh.reshape(2,-1).T
    inv=pca.inverse_transform(coords)
    grid_scores=-detector.score_samples(inv)
    zz=grid_scores.reshape(125,125)
    fig,ax=plt.subplots(figsize=(10,7))
    m=ax.contourf(xx,yy,zz,levels=25,cmap='viridis')
    fig.colorbar(m,ax=ax,label='Score proyectado')
    if zz.min() < threshold < zz.max():
        ax.contour(xx,yy,zz,levels=[threshold],colors='orangered',linewidths=2)
    ax.scatter(z[:,0],z[:,1],s=4,alpha=.2,color='white',label='Train')
    ax.set(xlabel='PC1',ylabel='PC2',title='Corte PCA del detector (no es un intervalo de confianza)')
    ax.legend();save(fig,'05_espacio_PCA.png')

    # Perturbación progresiva con direcciones fijas; varias repeticiones para reducir azar.
    levels=np.arange(0,101,5)
    rng=np.random.default_rng(seed)
    base=test[FEATURES].to_numpy(dtype=float)
    iqr=(train[FEATURES].quantile(.75)-train[FEATURES].quantile(.25)).to_numpy(dtype=float)
    # IQR cero en señales discretizadas: escala de respaldo fundada en train.
    fallback=train[FEATURES].std().to_numpy(dtype=float)
    scales=np.where(iqr>1e-9,iqr,np.maximum(fallback,1e-6))
    # 100% = 3 veces la escala robusta de cada variable (definición experimental).
    repeats=12
    directions=rng.normal(0,1,size=(repeats,*base.shape))
    curves=[]
    noise_scores=[]
    for pct in levels:
        rate=[];mean_scores=[]
        for direction in directions:
            pert=base+(pct/100)*3*scales*direction
            scores=-pipeline.score_samples(pd.DataFrame(pert,columns=FEATURES))
            rate.append(np.mean(scores>threshold))
            mean_scores.append(np.median(scores))
        curves.append(rate)
        noise_scores.append(mean_scores)
    curves=np.asarray(curves);noise_scores=np.asarray(noise_scores)
    results=pd.DataFrame({'ruido_pct':levels,
      'tasa_media_pct':100*curves.mean(axis=1),
      'tasa_p10_pct':100*np.quantile(curves,.1,axis=1),
      'tasa_p90_pct':100*np.quantile(curves,.9,axis=1),
      'score_mediana':np.median(noise_scores,axis=1)})
    results.to_csv(BASE/'results'/'ruido_0_a_100.csv',index=False)

    fig,ax=plt.subplots(figsize=(9,5))
    ax.plot(levels,results.tasa_media_pct,marker='o',label='Tasa detectada')
    ax.fill_between(levels,results.tasa_p10_pct,results.tasa_p90_pct,alpha=.2,label='Rango p10–p90 (12 repeticiones)')
    ax.set(xlabel='Intensidad de ruido sintético (%)',ylabel='Muestras marcadas anómalas (%)',ylim=(-3,103),title='Sensibilidad al ruido: no es probabilidad calibrada')
    ax.grid(alpha=.2);ax.legend();save(fig,'06_ruido_tasa_anomalias.png')

    fig,ax=plt.subplots(figsize=(9,5))
    ax.plot(levels,results.score_mediana,marker='o')
    ax.axhline(threshold,color='crimson',ls='--',label='Umbral')
    ax.set(xlabel='Intensidad de ruido (%)',ylabel='Mediana score de anomalía',title='Respuesta del modelo a perturbaciones crecientes')
    ax.legend();ax.grid(alpha=.2);save(fig,'07_ruido_vs_score.png')

    # Trayectoria de una misma muestra, mismas perturbaciones, coordenadas PCA.
    sample=base[0];direction=directions[0,0]
    points=np.array([sample+(pct/100)*3*scales*direction for pct in levels])
    z_path=pca.transform(scaler.transform(imp.transform(pd.DataFrame(points,columns=FEATURES))))
    point_scores=-pipeline.score_samples(pd.DataFrame(points,columns=FEATURES))
    fig,ax=plt.subplots(figsize=(10,7))
    m=ax.contourf(xx,yy,zz,levels=25,cmap='viridis',alpha=.65)
    fig.colorbar(m,ax=ax,label='Score (corte PCA)')
    if zz.min()<threshold<zz.max(): ax.contour(xx,yy,zz,levels=[threshold],colors='orangered')
    ax.plot(z_path[:,0],z_path[:,1],'-o',color='white',markeredgecolor='black')
    for k in [0,5,10,15,20]:
        ax.annotate(f'{levels[k]}%',(z_path[k,0],z_path[k,1]),xytext=(4,5),textcoords='offset points')
    ax.set(xlabel='PC1',ylabel='PC2',title='Trayectoria de una muestra con ruido creciente (proyección PCA)')
    save(fig,'08_trayectoria_PCA.png')

    report={'dataset_rows_raw':int(len(raw)),'valid_rows':int(len(valid)),
        'on_rows':int(n),'train_rows':int(len(train)),'calibration_rows':int(len(calib)),
        'test_rows':int(len(test)),'model':'IsolationForest (CPU)',
        'features':FEATURES,'calibration_threshold_p99':threshold,
        'test_flag_rate_pct':100*false_alarm,
        'best_trial':best_artifact['trial'],'best_hyperparameters':best_artifact['selected_params'],
        'best_validation_metrics':best_artifact['selection_metrics'],
        'last_trial':last_artifact['trial'],'last_hyperparameters':last_artifact['selected_params'],
        'model_selection_note':'Seleccion basada en falsos positivos normales y anomalías sinteticas; sin etiquetas reales no prueba eficacia ante fallas.',
        'pca_explained_variance_ratio':pca.explained_variance_ratio_.tolist(),
        'limitations':['Estados missing/unknown excluidos del entrenamiento',
                       'Sin fallas reales etiquetadas no se puede medir recall ni precisión',
                       'Tasa de detección no es probabilidad de anomalía',
                       'No usar como único mecanismo de apagado de seguridad']}
    (BASE/'results'/'metrics.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(report,indent=2,ensure_ascii=False))
    print('Gráficos:',BASE/'plots',
          '\nBest:',BASE/'models'/'best_model.joblib',
          '\nLast:',BASE/'models'/'last_model.joblib')

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--csv',type=Path,default=BASE/'data'/'VentiSet.csv')
    parser.add_argument('--seed',type=int,default=42)
    args=parser.parse_args()
    main(args.csv,args.seed)
