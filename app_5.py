import streamlit as st
import pandas as pd
import folium
from folium.plugins import HeatMap
import geopandas as gpd
from streamlit_folium import st_folium
import io

# =========================
# CONFIG STREAMLIT
# =========================
st.set_page_config(page_title="Cobertura vs Ventas", layout="wide")
st.title("📡 Análisis de Comportamiento: Mercado vs Feria")

# =========================
# CACHE LECTURA ARCHIVOS
# =========================
@st.cache_data
def cargar_datos():
    ventas = pd.read_excel("data/ventas.xlsx", dtype={"LOGIN": str})
    proactivos = pd.read_excel("data/proactivos.xlsx", dtype={"LOGIN": str})
    return ventas, proactivos

ventas, proactivos = cargar_datos()

# =========================
# LIMPIEZA INICIAL Y PREPARACIÓN
# =========================
ventas["LOGIN"] = ventas["LOGIN"].astype(str).str.strip()
proactivos["LOGIN"] = proactivos["LOGIN"].astype(str).str.strip()

ventas["REQUESTDATE"] = pd.to_datetime(
    ventas["REQUESTDATE"],
    dayfirst=True,
    errors="coerce"
)

# Columna oculta para rastrear la geografía a nivel transacción
ventas["TIPO_GEO"] = "Fuera de Rango" 

def limpiar_coordenadas(df, lat="LATITUDE", lon="LONGITUDE"):
    for c in [lat, lon]:
        df[c] = (
            df[c].astype(str)
            .str.replace("'", "", regex=False)
            .str.strip()
            .replace("", pd.NA)
        )
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=[lat, lon])

ventas = limpiar_coordenadas(ventas)
proactivos = limpiar_coordenadas(proactivos)

# =========================
# CACHE GEOPROCESAMIENTO
# =========================
@st.cache_data
def procesar_geodatos(ventas, proactivos):
    ventas_gdf = gpd.GeoDataFrame(
        ventas,
        geometry=gpd.points_from_xy(ventas["LONGITUDE"], ventas["LATITUDE"]),
        crs="EPSG:4326"
    ).to_crs(epsg=32718)

    proactivos_gdf = gpd.GeoDataFrame(
        proactivos,
        geometry=gpd.points_from_xy(proactivos["LONGITUDE"], proactivos["LATITUDE"]),
        crs="EPSG:4326"
    ).to_crs(epsg=32718)

    ventas_proac = gpd.sjoin_nearest(
        ventas_gdf, proactivos_gdf[["LOGIN", "geometry"]],
        how="left", distance_col="distancia_m"
    )
    # Actualizado a 500m
    ventas_proac = ventas_proac[ventas_proac["distancia_m"] <= 500]
    return ventas_gdf, proactivos_gdf, ventas_proac

ventas_gdf, proactivos_gdf, ventas_proac = procesar_geodatos(ventas, proactivos)

# =========================
# LÓGICA CORE: CLÚSTER, TIEMPO Y DÍAS
# =========================
DIAS_MAP = {0: "Lun", 1: "Mar", 2: "Mié", 3: "Jue", 4: "Vie", 5: "Sáb", 6: "Dom"}

def evaluar_comportamiento(df_ventas):
    total = len(df_ventas)
    if total == 0:
        return 0, 0, 0.0, "Sin Ventas", "-", "-"
    
    df_validas = df_ventas.dropna(subset=["REQUESTDATE"])
    fechas_unicas = sorted(df_validas["REQUESTDATE"].dt.date.unique())
    dias_semana_nums = df_validas["REQUESTDATE"].dt.dayofweek.unique()
    
    dias = len(fechas_unicas)
    if dias == 0:
        return total, 0, 0.0, "Desconocido", "-", "-"
        
    dias_semana_nums = sorted(dias_semana_nums)
    dias_str = ", ".join([DIAS_MAP[d] for d in dias_semana_nums])
    dias_mes_str = ", ".join([str(f.day) for f in fechas_unicas])
    
    promedio = total / dias if dias > 0 else 0
    solo_fines_de_semana = all(d in [4, 5, 6] for d in dias_semana_nums)
    
    if dias <= 2 or solo_fines_de_semana:
        comportamiento = "🎪 Feria"
    else:
        comportamiento = "🏪 Mercado"
    
    return total, dias, promedio, comportamiento, dias_str, dias_mes_str

# Actualizado a 500m de radio base y siguientes
def calcular_cadena_ventas(geom_vendedor, df_ventas_login, r_inicial=500, r_siguientes=500):
    if df_ventas_login.empty:
        return {}, [], [], []
    
    idx_merc = []
    idx_feria = []
    
    dist_base = df_ventas_login.geometry.distance(geom_vendedor)
    ventas_base = df_ventas_login[dist_base <= r_inicial]
    tot_b, dias_b, prom_b, comp_b, d_str_b, d_mes_b = evaluar_comportamiento(ventas_base)
    
    if "Mercado" in comp_b:
        idx_merc.extend(ventas_base.index.tolist())
    else:
        idx_feria.extend(ventas_base.index.tolist())
    
    cluster_base = {
        "total": tot_b, "dias": dias_b, "promedio": prom_b, 
        "comportamiento": comp_b, "dias_str": d_str_b, "dias_mes": d_mes_b
    }
    
    # GUARDAR FECHAS: Memoria para detectar Anexos
    fechas_acumuladas = set(ventas_base["REQUESTDATE"].dt.date.dropna())
    
    cadena = []
    centro_actual = geom_vendedor
    zonas_exclusion = [(centro_actual, r_inicial)]
    df_disponible = df_ventas_login.copy()
    
    while True:
        fuera_de_zonas = pd.Series(True, index=df_disponible.index)
        for centro, radio in zonas_exclusion:
            fuera_de_zonas &= (df_disponible.geometry.distance(centro) > radio)
        
        df_filtrado = df_disponible[fuera_de_zonas]
        if df_filtrado.empty:
            break
            
        distancias = df_filtrado.geometry.distance(centro_actual)
        idx_max = distancias.idxmax()
        venta_lejana = df_filtrado.loc[idx_max]
        dist_max = distancias.loc[idx_max]
        
        nuevo_centro = venta_lejana.geometry
        ventas_salto = df_filtrado[df_filtrado.geometry.distance(nuevo_centro) <= r_siguientes]
        
        tot_s, dias_s, prom_s, comp_s, d_str_s, d_mes_s = evaluar_comportamiento(ventas_salto)
        
        # LÓGICA TEMPORAL: Detectar Anexos vs Saltos Reales
        fechas_salto = set(ventas_salto["REQUESTDATE"].dt.date.dropna())
        es_anexo = False
        
        if fechas_salto and fechas_salto.issubset(fechas_acumuladas):
            es_anexo = True
            comp_s = "📍 Anexo GPS (Mismo Día)"
            # Hereda la clasificación del clúster base
            if "Mercado" in comp_b:
                idx_merc.extend(ventas_salto.index.tolist())
            else:
                idx_feria.extend(ventas_salto.index.tolist())
        else:
            # Si aporta días nuevos, se actualiza el acumulado
            fechas_acumuladas.update(fechas_salto)
            if "Mercado" in comp_s:
                idx_merc.extend(ventas_salto.index.tolist())
            else:
                idx_feria.extend(ventas_salto.index.tolist())
        
        cadena.append({
            "punto_origen": centro_actual,
            "punto_destino": nuevo_centro,
            "distancia": dist_max,
            "total": tot_s,
            "dias": dias_s,
            "promedio": prom_s,
            "comportamiento": comp_s,
            "dias_str": d_str_s,
            "dias_mes": d_mes_s,
            "es_anexo": es_anexo
        })
        
        centro_actual = nuevo_centro
        zonas_exclusion.append((centro_actual, r_siguientes))
        
    return cluster_base, cadena, idx_merc, idx_feria

# =========================
# FILTROS DE INTERFAZ
# =========================
acciones = sorted(proactivos["Acción"].dropna().unique())
accion_seleccionada = st.selectbox("Acción Oficial", acciones)

departamentos = sorted(
    proactivos.loc[proactivos["Acción"] == accion_seleccionada, "Departamento"].dropna().unique()
)
depto_seleccionado = st.selectbox("Departamento", departamentos)

vendedores_filtrados = proactivos[
    (proactivos["Acción"] == accion_seleccionada) & (proactivos["Departamento"] == depto_seleccionado)
]
vendedor_seleccionado = st.selectbox("Seleccionar Vendedor Proactivo (LOGIN)", vendedores_filtrados["LOGIN"].sort_values())

# =========================
# PROCESAR DATOS DE LA UI
# =========================
ventas_login = ventas[ventas["LOGIN"] == str(vendedor_seleccionado).strip()].copy()
cluster_base = {}
cadena_pasos = []
saltos_reales = 0

# Variables por defecto para el mapa si no hay ventas
lat_vendedor = 0
lon_vendedor = 0

if not ventas_login.empty:
    # 1. Convertimos todas las ventas a un mapa geográfico
    ventas_geo_login = gpd.GeoDataFrame(
        ventas_login,
        geometry=gpd.points_from_xy(ventas_login["LONGITUDE"], ventas_login["LATITUDE"]),
        crs="EPSG:4326"
    ).to_crs(epsg=32718)

    # 2. ANCLAJE POR DENSIDAD: Buscamos qué venta agrupa a la mayor cantidad de vecinos a 500m
    conteos_vecinos = ventas_geo_login.geometry.apply(
        lambda geom: (ventas_geo_login.geometry.distance(geom) <= 500).sum()
    )
    idx_max_volumen = conteos_vecinos.idxmax()
    
    # 3. Establecemos esa posición dominante como el Origen de todo el mapa
    venta_principal = ventas_login.loc[idx_max_volumen]
    lat_vendedor = venta_principal["LATITUDE"]
    lon_vendedor = venta_principal["LONGITUDE"]
    vendedor_geo = ventas_geo_login.loc[idx_max_volumen].geometry

    # Ejecutamos el clúster con este nuevo origen robusto
    cluster_base, cadena_pasos, _, _ = calcular_cadena_ventas(vendedor_geo, ventas_geo_login)

    # Calculamos cuántos saltos no son anexos
    saltos_reales = sum(1 for p in cadena_pasos if not p.get("es_anexo", False))
    st.subheader(f"🪜 Desglose de Operación: Posición Inicial + {saltos_reales} Saltos Reales")
    
    total_tarjetas = 1 + len(cadena_pasos)
    cols = st.columns(min(total_tarjetas, 4))
    
    with cols[0]:
        st.success(f"**📍 Posición Inicial: {cluster_base.get('comportamiento', 'N/A')}**\n"
                   f"- Ventas: {cluster_base.get('total', 0)}\n"
                   f"- Fechas: **{cluster_base.get('dias_mes', '-')}** ({cluster_base.get('dias_str', '-')})\n"
                   f"- Prom/Día: {cluster_base.get('promedio', 0):.1f}")
                   
    for i, paso in enumerate(cadena_pasos):
        with cols[(i + 1) % 4]:
            if paso.get("es_anexo", False):
                st.warning(f"**🔗 Anexo (Mismo Día)**\n"
                           f"- Distancia: {paso['distancia']:,.0f}m\n"
                           f"- Ventas: {paso['total']}\n"
                           f"- Fechas: **{paso['dias_mes']}**")
            else:
                st.info(f"**🏃 Salto {i+1}: {paso['comportamiento']}**\n"
                        f"- Distancia: {paso['distancia']:,.0f}m\n"
                        f"- Ventas: {paso['total']}\n"
                        f"- Fechas: **{paso['dias_mes']}** ({paso['dias_str']})\n"
                        f"- Prom/Día: {paso['promedio']:,.1f}")

# =========================
# MAPA FOLIUM
# =========================
# Evitamos renderizar un mapa en [0,0] si no hay datos
if lat_vendedor != 0 and lon_vendedor != 0:
    m = folium.Map(location=[lat_vendedor, lon_vendedor], zoom_start=14)

    ventas_heat = ventas.dropna(subset=["LATITUDE", "LONGITUDE"])
    HeatMap(ventas_heat[["LATITUDE", "LONGITUDE"]].values.tolist(), radius=15, blur=20).add_to(m)

    folium.CircleMarker(
        location=[lat_vendedor, lon_vendedor], radius=8, color="blue", fill=True,
        popup=f"<b>Posición Inicial Real (Mayor Densidad)</b><br>Comportamiento: {cluster_base.get('comportamiento', 'N/A')}<br>Fechas: {cluster_base.get('dias_mes', '-')} ({cluster_base.get('dias_str', '-')})"
    ).add_to(m)
    # Radio actualizado a 500m
    folium.Circle(
        location=[lat_vendedor, lon_vendedor], radius=500, color="blue", fill=False, opacity=0.4
    ).add_to(m)

    for idx, paso in enumerate(cadena_pasos):
        orig_wgs = gpd.GeoSeries([paso["punto_origen"]], crs="EPSG:32718").to_crs(epsg=4326).iloc[0]
        dest_wgs = gpd.GeoSeries([paso["punto_destino"]], crs="EPSG:32718").to_crs(epsg=4326).iloc[0]
        
        if paso.get("es_anexo", False):
            color_icono = "gray"
            peso_linea = 2
            estilo_linea = "10, 10"
        else:
            color_icono = "green" if "Mercado" in paso["comportamiento"] else "orange"
            peso_linea = 4
            estilo_linea = None
            
        folium.PolyLine(
            locations=[[orig_wgs.y, orig_wgs.x], [dest_wgs.y, dest_wgs.x]],
            color="black", weight=peso_linea, opacity=0.85, dash_array=estilo_linea
        ).add_to(m)
        
        folium.Marker(
            location=[dest_wgs.y, dest_wgs.x],
            popup=f"<b>{'Anexo' if paso.get('es_anexo') else f'Salto {idx+1}'}: {paso['comportamiento']}</b><br>Ventas: {paso['total']}<br>Fechas: {paso['dias_mes']} ({paso['dias_str']})",
            icon=folium.Icon(color=color_icono, icon="info-sign")
        ).add_to(m)
        
        # Radio actualizado a 500m
        folium.Circle(
            location=[dest_wgs.y, dest_wgs.x], radius=500, color=color_icono, fill=False, dash_array="5, 5"
        ).add_to(m)

    st_folium(m, width=1500, height=650, returned_objects=[])
else:
    st.warning("No hay ventas registradas para generar el mapa.")

# =========================
# TABLA RESUMEN EJECUTIVO (VISUAL STREAMLIT)
# =========================
st.subheader("📊 Resumen Ejecutivo (Clasificación Oficial vs Real)")

resumen_login = []

for _, vendedor_row in proactivos.iterrows():
    login = str(vendedor_row["LOGIN"]).strip()
    ventas_login_all = ventas[ventas["LOGIN"] == login].copy()
    
    if ventas_login_all.empty:
        continue
        
    # ANCLAJE PARA EXCEL: Por Densidad a 500m
    ventas_gdf_box = gpd.GeoDataFrame(
        ventas_login_all,
        geometry=gpd.points_from_xy(ventas_login_all["LONGITUDE"], ventas_login_all["LATITUDE"]), crs="EPSG:4326"
    ).to_crs(epsg=32718)
    
    conteos_vecinos = ventas_gdf_box.geometry.apply(
        lambda geom: (ventas_gdf_box.geometry.distance(geom) <= 500).sum()
    )
    idx_max_volumen = conteos_vecinos.idxmax()
    
    geom_vend = ventas_gdf_box.loc[idx_max_volumen].geometry
    
    c_base, c_saltos, i_merc, i_feria = calcular_cadena_ventas(geom_vend, ventas_gdf_box)
    
    ventas.loc[i_merc, "TIPO_GEO"] = "Mercado"
    ventas.loc[i_feria, "TIPO_GEO"] = "Feria"
    
    v_mercado = 0
    v_feria = 0
    
    if "Mercado" in c_base.get("comportamiento", ""):
        v_mercado += c_base.get("total", 0)
    elif "Feria" in c_base.get("comportamiento", ""):
        v_feria += c_base.get("total", 0)
        
    for s in c_saltos:
        if "Mercado" in s["comportamiento"]:
            v_mercado += s["total"]
        else:
            v_feria += s["total"]

    total_general = v_mercado + v_feria
    
    saltos_reales_excel = sum(1 for s in c_saltos if not s.get("es_anexo", False))

    resumen_login.append({
        "LOGIN": login, 
        "Departamento": vendedor_row["Departamento"],
        "Acción Oficial": vendedor_row["Acción"],
        "Comport. Inicial": c_base.get("comportamiento", "Sin Ventas"),
        "Total Saltos": saltos_reales_excel,
        "Ventas Tipo Mercado": v_mercado,
        "Ventas Tipo Feria": v_feria,
        "Total Ventas": total_general
    })

if resumen_login:
    resumen_login_df = pd.DataFrame(resumen_login)
    
    # MOSTRAR EN STREAMLIT
    df_visual = resumen_login_df.rename(columns={"LOGIN": "LOGIN (Vendedor)"})
    st.dataframe(
        df_visual.sort_values("Total Ventas", ascending=False),
        width="stretch",
        use_container_width=True
    )

    # =========================
    # LÓGICA EXCEL Y REGLAS DE NEGOCIO POR DÍA
    # =========================
    ventas_validas = ventas.dropna(subset=["REQUESTDATE"]).copy()
    ventas_validas["Día"] = ventas_validas["REQUESTDATE"].dt.date
    
    df_diario = ventas_validas.groupby(["LOGIN", "Día"]).agg(
        Venta=("LOGIN", "count"),
        Geo_Mercado=("TIPO_GEO", lambda x: (x == "Mercado").sum()),
        Geo_Feria=("TIPO_GEO", lambda x: (x == "Feria").sum())
    ).reset_index()

    df_excel = pd.merge(
        df_diario, 
        resumen_login_df[["LOGIN", "Departamento", "Acción Oficial", "Comport. Inicial", "Total Saltos"]], 
        on="LOGIN", 
        how="inner"
    )

    df_excel["Ventas Tipo Mercado"] = df_excel["Geo_Mercado"]
    df_excel["Ventas Tipo Feria"] = df_excel["Geo_Feria"]
    df_excel["Total Ventas"] = df_excel["Venta"]

    def calc_vr_mercado(row):
        if str(row["Acción Oficial"]).strip().upper() == "MERCADO":
            return row["Venta"]
        else:
            if row["Total Saltos"] >= 5 and str(row["Departamento"]).strip().upper() != "LIMA":
                return 0
            return row["Ventas Tipo Mercado"]

    def calc_vr_feria(row):
        if str(row["Acción Oficial"]).strip().upper() == "MERCADO":
            return 0
        else:
            if row["Total Saltos"] >= 5 and str(row["Departamento"]).strip().upper() != "LIMA":
                return row["Venta"]
            return row["Ventas Tipo Feria"]

    df_excel["VENTA_REAL_MERCADO"] = df_excel.apply(calc_vr_mercado, axis=1)
    df_excel["VENTA_REAL_FERIA"] = df_excel.apply(calc_vr_feria, axis=1)
    
    df_mercado = df_excel[df_excel["VENTA_REAL_MERCADO"] > 0].copy()
    df_mercado["Etiqueta Reporte"] = "Mercado"
    df_mercado["Total Ventas Reporte"] = df_mercado["VENTA_REAL_MERCADO"]
    
    cols_vista = ["LOGIN", "Día", "Departamento", "Acción Oficial", "Etiqueta Reporte", "Total Ventas Reporte"]
    df_mercado = df_mercado[cols_vista]

    df_feria = df_excel[df_excel["VENTA_REAL_FERIA"] > 0].copy()
    df_feria["Etiqueta Reporte"] = "Feria"
    df_feria["Total Ventas Reporte"] = df_feria["VENTA_REAL_FERIA"]
    df_feria = df_feria[cols_vista]

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df_mercado.to_excel(writer, index=False, sheet_name='Reporte_Mercados')
        df_feria.to_excel(writer, index=False, sheet_name='Reporte_Ferias')
        resumen_login_df.to_excel(writer, index=False, sheet_name='Resumen_Consolidado')
        
        for sheet_name in ['Reporte_Mercados', 'Reporte_Ferias']:
            worksheet = writer.sheets[sheet_name]
            if sheet_name == 'Reporte_Mercados':
                worksheet.autofilter(0, 0, len(df_mercado), 5)
            else:
                worksheet.autofilter(0, 0, len(df_feria), 5)

    excel_data = output.getvalue()

    st.markdown("---")
    st.download_button(
        label="📥 Descargar Reportes Segmentados (Mercados y Ferias)",
        data=excel_data,
        file_name="Reporte_Ventas_Segmentado.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )