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
    ventas_proac = ventas_proac[ventas_proac["distancia_m"] <= 300]
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

def calcular_cadena_ventas(geom_vendedor, df_ventas_login, r_inicial=300, r_siguientes=300):
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
            "dias_mes": d_mes_s
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

fila_vendedor = proactivos.loc[proactivos["LOGIN"] == vendedor_seleccionado].iloc[0]
lat_vendedor, lon_vendedor = fila_vendedor["LATITUDE"], fila_vendedor["LONGITUDE"]

# =========================
# PROCESAR DATOS DE LA UI
# =========================
ventas_login = ventas[ventas["LOGIN"] == str(vendedor_seleccionado).strip()].copy()
cluster_base = {}
cadena_pasos = []

if not ventas_login.empty:
    vendedor_geo = gpd.GeoDataFrame(
        geometry=gpd.points_from_xy([lon_vendedor], [lat_vendedor]), crs="EPSG:4326"
    ).to_crs(epsg=32718).iloc[0].geometry

    ventas_geo_login = gpd.GeoDataFrame(
        ventas_login,
        geometry=gpd.points_from_xy(ventas_login["LONGITUDE"], ventas_login["LATITUDE"]),
        crs="EPSG:4326"
    ).to_crs(epsg=32718)

    cluster_base, cadena_pasos, _, _ = calcular_cadena_ventas(vendedor_geo, ventas_geo_login)

    st.subheader(f"🪜 Desglose de Operación: Posición Inicial + {len(cadena_pasos)} Saltos")
    
    total_tarjetas = 1 + len(cadena_pasos)
    cols = st.columns(min(total_tarjetas, 4))
    
    with cols[0]:
        st.success(f"**📍 Posición Inicial: {cluster_base.get('comportamiento', 'N/A')}**\n"
                   f"- Ventas: {cluster_base.get('total', 0)}\n"
                   f"- Fechas: **{cluster_base.get('dias_mes', '-')}** ({cluster_base.get('dias_str', '-')})\n"
                   f"- Prom/Día: {cluster_base.get('promedio', 0):.1f}")
                   
    for i, paso in enumerate(cadena_pasos):
        with cols[(i + 1) % 4]:
            st.info(f"**🏃 Salto {i+1}: {paso['comportamiento']}**\n"
                    f"- Distancia: {paso['distancia']:,.0f}m\n"
                    f"- Ventas: {paso['total']}\n"
                    f"- Fechas: **{paso['dias_mes']}** ({paso['dias_str']})\n"
                    f"- Prom/Día: {paso['promedio']:,.1f}")

# =========================
# MAPA FOLIUM
# =========================
m = folium.Map(location=[lat_vendedor, lon_vendedor], zoom_start=14)

ventas_heat = ventas.dropna(subset=["LATITUDE", "LONGITUDE"])
HeatMap(ventas_heat[["LATITUDE", "LONGITUDE"]].values.tolist(), radius=15, blur=20).add_to(m)

folium.CircleMarker(
    location=[lat_vendedor, lon_vendedor], radius=8, color="blue", fill=True,
    popup=f"<b>Posición Inicial</b><br>Comportamiento: {cluster_base.get('comportamiento', 'N/A')}<br>Fechas: {cluster_base.get('dias_mes', '-')} ({cluster_base.get('dias_str', '-')})"
).add_to(m)
folium.Circle(
    location=[lat_vendedor, lon_vendedor], radius=300, color="blue", fill=False, opacity=0.4
).add_to(m)

for idx, paso in enumerate(cadena_pasos):
    orig_wgs = gpd.GeoSeries([paso["punto_origen"]], crs="EPSG:32718").to_crs(epsg=4326).iloc[0]
    dest_wgs = gpd.GeoSeries([paso["punto_destino"]], crs="EPSG:32718").to_crs(epsg=4326).iloc[0]
    
    folium.PolyLine(
        locations=[[orig_wgs.y, orig_wgs.x], [dest_wgs.y, dest_wgs.x]],
        color="black", weight=4, opacity=0.85
    ).add_to(m)
    
    color_icono = "green" if "Mercado" in paso["comportamiento"] else "orange"
    
    folium.Marker(
        location=[dest_wgs.y, dest_wgs.x],
        popup=f"<b>Salto {idx+1}: {paso['comportamiento']}</b><br>Ventas: {paso['total']}<br>Fechas: {paso['dias_mes']} ({paso['dias_str']})",
        icon=folium.Icon(color=color_icono, icon="info-sign")
    ).add_to(m)
    
    folium.Circle(
        location=[dest_wgs.y, dest_wgs.x], radius=300, color=color_icono, fill=False, dash_array="5, 5"
    ).add_to(m)

st_folium(m, width=1500, height=650, returned_objects=[])

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
        
    vendedor_gdf_box = gpd.GeoDataFrame(
        geometry=gpd.points_from_xy([vendedor_row["LONGITUDE"]], [vendedor_row["LATITUDE"]]), crs="EPSG:4326"
    ).to_crs(epsg=32718)
    
    ventas_gdf_box = gpd.GeoDataFrame(
        ventas_login_all,
        geometry=gpd.points_from_xy(ventas_login_all["LONGITUDE"], ventas_login_all["LATITUDE"]), crs="EPSG:4326"
    ).to_crs(epsg=32718)
    
    geom_vend = vendedor_gdf_box.iloc[0].geometry
    
    c_base, c_saltos, i_merc, i_feria = calcular_cadena_ventas(geom_vend, ventas_gdf_box)
    
    # Etiquetamos silenciosamente las transacciones para el Excel
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

    resumen_login.append({
        "LOGIN": login, 
        "Departamento": vendedor_row["Departamento"],
        "Acción Oficial": vendedor_row["Acción"],
        "Comport. Inicial": c_base.get("comportamiento", "Sin Ventas"),
        "Total Saltos": len(c_saltos),
        "Ventas Tipo Mercado": v_mercado,
        "Ventas Tipo Feria": v_feria,
        "Total Ventas": total_general
    })

if resumen_login:
    resumen_login_df = pd.DataFrame(resumen_login)
    
    # 1. MOSTRAR EN STREAMLIT (SE MANTIENE EL RESUMEN MENSUAL)
    df_visual = resumen_login_df.rename(columns={"LOGIN": "LOGIN (Vendedor)"})
    st.dataframe(
        df_visual.sort_values("Total Ventas", ascending=False),
        width="stretch",
        use_container_width=True
    )

    # =========================
    # LÓGICA EXCEL Y REGLAS DE NEGOCIO POR DÍA (COORDINADORES)
    # =========================
    ventas_validas = ventas.dropna(subset=["REQUESTDATE"]).copy()
    ventas_validas["Día"] = ventas_validas["REQUESTDATE"].dt.date
    
    # Agrupamos por día y sumamos las etiquetas geográficas exactas de cada venta
    df_diario = ventas_validas.groupby(["LOGIN", "Día"]).agg(
        Venta=("LOGIN", "count"),
        Geo_Mercado=("TIPO_GEO", lambda x: (x == "Mercado").sum()),
        Geo_Feria=("TIPO_GEO", lambda x: (x == "Feria").sum())
    ).reset_index()

    # Unimos con los datos oficiales del vendedor
    df_excel = pd.merge(
        df_diario, 
        resumen_login_df[["LOGIN", "Departamento", "Acción Oficial", "Comport. Inicial", "Total Saltos"]], 
        on="LOGIN", 
        how="inner"
    )

    # Transformamos las métricas geográficas en las columnas visuales que pediste
    df_excel["Ventas Tipo Mercado"] = df_excel["Geo_Mercado"]
    df_excel["Ventas Tipo Feria"] = df_excel["Geo_Feria"]
    df_excel["Total Ventas"] = df_excel["Venta"] # Igual a Venta Diaria para evitar duplicados

    # REGLA 1: MERCADO AL NIVEL DIARIO
    def calc_vr_mercado(row):
        if str(row["Acción Oficial"]).strip().upper() == "MERCADO":
            return row["Venta"] # Toda la venta del día
        else:
            if row["Total Saltos"] >= 5 and str(row["Departamento"]).strip().upper() != "LIMA":
                return 0
            return row["Geo_Mercado"] # Divide la porción exacta de ese día

    # REGLA 2: FERIA AL NIVEL DIARIO
    def calc_vr_feria(row):
        if str(row["Acción Oficial"]).strip().upper() == "MERCADO":
            return 0
        else:
            if row["Total Saltos"] >= 5 and str(row["Departamento"]).strip().upper() != "LIMA":
                return row["Venta"] # Toda la venta del día
            return row["Geo_Feria"] # Divide la porción exacta de ese día

# =========================================================================
    # REEMPLAZAR DESDE AQUÍ: CÁLCULO Y GENERACIÓN DE EXCEL SEGMENTADO
    # =========================================================================
    
    # 1. Definir las reglas de negocio para calcular las ventas reales
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

    # 2. Aplicar los cálculos al DataFrame df_excel
    df_excel["VENTA_REAL_MERCADO"] = df_excel.apply(calc_vr_mercado, axis=1)
    df_excel["VENTA_REAL_FERIA"] = df_excel.apply(calc_vr_feria, axis=1)
    
    # 3. Crear el Reporte exclusivo de MERCADOS
    df_mercado = df_excel[df_excel["VENTA_REAL_MERCADO"] > 0].copy()
    df_mercado["Etiqueta Reporte"] = "Mercado"
    df_mercado["Total Ventas Reporte"] = df_mercado["VENTA_REAL_MERCADO"]
    
    # Columnas limpias para el reporte de Mercado
    cols_vista = ["LOGIN", "Día", "Departamento", "Acción Oficial", "Etiqueta Reporte", "Total Ventas Reporte"]
    df_mercado = df_mercado[cols_vista]

    # 4. Crear el Reporte exclusivo de FERIAS
    df_feria = df_excel[df_excel["VENTA_REAL_FERIA"] > 0].copy()
    df_feria["Etiqueta Reporte"] = "Feria"
    df_feria["Total Ventas Reporte"] = df_feria["VENTA_REAL_FERIA"]
    df_feria = df_feria[cols_vista]

    # 5. GENERAR EL EXCEL EN MEMORIA CON MÚLTIPLES HOJAS
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df_mercado.to_excel(writer, index=False, sheet_name='Reporte_Mercados')
        df_feria.to_excel(writer, index=False, sheet_name='Reporte_Ferias')
        resumen_login_df.to_excel(writer, index=False, sheet_name='Resumen_Consolidado')
        
        # Formato básico para ayudar a los coordinadores
        for sheet_name in ['Reporte_Mercados', 'Reporte_Ferias']:
            worksheet = writer.sheets[sheet_name]
            # Autofiltros activos
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