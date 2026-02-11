import os
import pyodbc
import re
from datetime import date, datetime
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential

# ==========================================
# 1. CONFIGURACIÓN
# ==========================================
ENDPOINT = ""
KEY = ""
CARPETA_FACTURAS = r""

MODEL_ID = "modelo-eleia" 

SERVER = ''
DATABASE = ''
USERNAME = ''
PASSWORD = ''
DRIVER = ''
MI_CORREO = "" 

# ==========================================
# 2. FUNCIONES DE LIMPIEZA (VERSION PRO)
# ==========================================

def limpiar_fecha_objeto(valor):
    """
    Convierte cualquier fecha loca ("12 noviembre 2024", "01/10/24") en objeto date real.
    """
    if not valor: return None
    
    # 1. Si Azure ya nos da la fecha procesada
    if hasattr(valor, 'valueDate') and valor.valueDate:
        return valor.valueDate

    # 2. Obtenemos el texto limpio
    texto = valor.content if hasattr(valor, 'content') else str(valor)
    if not texto: return None
    texto = str(texto).lower().strip()

    # Mapa de meses
    meses_map = {
        "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
        "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12
    }

    try:
        # CASO 1: Formato texto "12 noviembre 2024" o "12 de noviembre de 2024"
        # Regex: Busca digitos + espacio/de + letras + espacio/de + 4 digitos
        match_texto = re.search(r'(\d{1,2})\s*(?:de)?\s*([a-z]+)\s*(?:de)?\s*(\d{4})', texto)
        if match_texto:
            dia = int(match_texto.group(1))
            mes_nombre = match_texto.group(2)
            anio = int(match_texto.group(3))
            
            if mes_nombre in meses_map:
                return date(anio, meses_map[mes_nombre], dia)

        # CASO 2: Formato numérico "01/10/2024" o "01-10-2024"
        match_num = re.search(r'(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{2,4})', texto)
        if match_num:
            dia = int(match_num.group(1))
            mes = int(match_num.group(2))
            anio = int(match_num.group(3))
            if anio < 100: anio += 2000 # Corregir años cortos (24 -> 2024)
            return date(anio, mes, dia)

    except Exception:
        return None
        
    return None

def limpiar_fecha_sql(valor):
    """Devuelve string YYYY-MM-DD para SQL o la fecha de hoy si falla"""
    obj = limpiar_fecha_objeto(valor)
    if obj:
        return obj.strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")

def calcular_dias(inicio, fin):
    """Resta dos fechas usando la función robusta"""
    d1 = limpiar_fecha_objeto(inicio)
    d2 = limpiar_fecha_objeto(fin)
    if d1 and d2: return abs((d2 - d1).days)
    return None

def limpiar_contrato_solo_numeros(valor):
    """Deja solo números (quita paréntesis, letras y espacios)"""
    if not valor: return None
    texto = valor.content if hasattr(valor, 'content') else str(valor)
    if not texto: return None
    
    # Regex: Reemplaza todo lo que NO sea número por vacío
    solo_numeros = re.sub(r'\D', '', str(texto))
    if not solo_numeros: return None
    return solo_numeros[:50]

def limpiar_texto(valor, max_len=100):
    if not valor: return None
    texto = valor.content if hasattr(valor, 'content') else str(valor)
    if not texto: return None
    
    texto = str(texto).replace("\n", " ").replace("\r", "").replace("\t", " ")
    texto = " ".join(texto.split())
    if len(texto) > max_len: texto = texto[:max_len]
    return texto.strip()

def limpiar_decimal(valor):
    if valor is None: return None
    if hasattr(valor, 'content') and valor.content is None: return None
    texto = valor.content if hasattr(valor, 'content') else str(valor)
    if not texto: return None
    
    texto = str(texto).replace("€", "").replace("kWh", "").replace("kW", "").replace("días", "").replace("día", "").strip()
    texto = texto.replace(".", "").replace(",", ".") 
    try: return float(texto)
    except: return None

def limpiar_entero(valor):
    d = limpiar_decimal(valor)
    return int(d) if d is not None else None

# ==========================================
# 3. ANÁLISIS DE FACTURA
# ==========================================
def analizar_factura_custom(client, file_path):
    print(f"   🔄 Procesando: {os.path.basename(file_path)}...")
    
    with open(file_path, "rb") as f:
        poller = client.begin_analyze_document(model_id=MODEL_ID, body=f)
    result = poller.result()
    
    campos = {}
    if result.documents:
        doc = result.documents[0]
        f = doc.fields 
        
        # --- DATOS GENERALES ---
        campos['NumFactura'] = f.get("NumFactura", {}).get("content", "S/N")
        
        # AQUI ES DONDE FALLABA: Ahora usa la nueva lógica Regex
        campos['FechaFactura'] = limpiar_fecha_sql(f.get("Fecha"))
        
        campos['Cliente'] = limpiar_texto(f.get("Cliente"), 150) or "Cliente Desconocido"
        campos['NIF_cliente'] = limpiar_texto(f.get("NIF cliente"), 20) or "00000000T"
        campos['Direccion'] = limpiar_texto(f.get("Diercción cliente"), 150) or "Dirección Desconocida"
        
        # CP
        field_cp = f.get("CP cliente")
        if field_cp and field_cp.get("content"):
            raw_cp = str(field_cp.get("content"))
            campos['CP'] = raw_cp.replace(",", "").replace(".", "").replace(" ", "").strip()[:5]
        else:
            campos['CP'] = None

        campos['Poblacion'] = limpiar_texto(f.get("Población cliente"), 100)
        campos['Provincia'] = limpiar_texto(f.get("Provincia cliente"), 50)

        # --- CONTRATO Y TARIFAS ---
        contrato_raw = f.get("Contrato") or f.get("ReferenciaContrato")
        campos['Contrato'] = limpiar_contrato_solo_numeros(contrato_raw) 
        
        campos['CUPS'] = limpiar_texto(f.get("CUPS"), 50)
        campos['Tarifa'] = limpiar_texto(f.get("Tarifa"), 10)
        campos['Base_imponible'] = limpiar_decimal(f.get("ImporteTotal"))

        # --- CALCULO DIAS ---
        # Usa FechaInicio y FechaFinal que suelen venir como 01/10/2024
        campos['DiasFactura'] = calcular_dias(f.get("FechaInicio"), f.get("FechaFinal"))

        # --- POTENCIAS Y CONSUMOS ---
        for i in range(1, 7):
            campos[f'PotenciaP{i}'] = limpiar_decimal(f.get(f"PotenciaP{i}"))
            campos[f'ConsumoP{i}'] = limpiar_entero(f.get(f"ConsumoP{i}"))
            campos[f'PrecioPotenciaP{i}'] = limpiar_decimal(f.get(f"PrecioPotenciaP{i}"))
            campos[f'PrecioEnergiaP{i}'] = limpiar_decimal(f.get(f"PrecioEnergiaP{i}"))

    return campos

# ==========================================
# 4. INSERTAR EN SQL SERVER
# ==========================================
def insertar_en_bd(datos, nombre_fichero):
    connection_string = f'DRIVER={DRIVER};SERVER={SERVER};DATABASE={DATABASE};UID={USERNAME};PWD={PASSWORD}'
    try:
        conn = pyodbc.connect(connection_string)
        cursor = conn.cursor()

        query = """
        INSERT INTO [dbo].[Factura] (
            [CorreoAlumno], [Nombrefichero], [NumFactura], [FechaFactura], 
            [Cliente], [NIF cliente], [Comercializadora], [NIF comercializadora], 
            [Diercción cliente], [Población cliente], [Provincia cliente], [CP cliente],
            [Contrato], [CUPS], [Tarifa], [Base imponible], [TipoFactura],
            [Días factura],
            [Potencia contratada kW P1], [Potencia contratada kW P2], [Potencia contratada kW P3],
            [Potencia contratada kW P4], [Potencia contratada kW P5], [Potencia contratada kW P6],
            [Consumo P1 kWh], [Consumo P2 kWh], [Consumo P3 kWh],
            [Consumo P4 kWh], [Consumo P5 kWh], [Consumo P6 kWh],
            [Precio P1 kW/día], [Precio P2 kW/día], [Precio P3 kW/día],
            [Precio P4 kW/día], [Precio P5 kW/día], [Precio P6 kW/día],
            [Precio E1 kWh], [Precio E2 kWh], [Precio E3 kWh],
            [Precio E4 kWn], [Precio E5 kWh], [Precio E6 kWh]
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        
        valores = (
            MI_CORREO, nombre_fichero, datos.get('NumFactura'), datos.get('FechaFactura'),
            datos.get('Cliente'), datos.get('NIF_cliente'), 'Eleia Energia', 'B88181441', 
            datos.get('Direccion'), datos.get('Poblacion'), datos.get('Provincia'), datos['CP'],
            datos.get('Contrato'), datos.get('CUPS'), datos.get('Tarifa'), datos.get('Base_imponible'), 'Luz',
            datos.get('DiasFactura'),
            datos.get('PotenciaP1'), datos.get('PotenciaP2'), datos.get('PotenciaP3'),
            datos.get('PotenciaP4'), datos.get('PotenciaP5'), datos.get('PotenciaP6'),
            datos.get('ConsumoP1'), datos.get('ConsumoP2'), datos.get('ConsumoP3'),
            datos.get('ConsumoP4'), datos.get('ConsumoP5'), datos.get('ConsumoP6'),
            datos.get('PrecioPotenciaP1'), datos.get('PrecioPotenciaP2'), datos.get('PrecioPotenciaP3'),
            datos.get('PrecioPotenciaP4'), datos.get('PrecioPotenciaP5'), datos.get('PrecioPotenciaP6'),
            datos.get('PrecioEnergiaP1'), datos.get('PrecioEnergiaP2'), datos.get('PrecioEnergiaP3'),
            datos.get('PrecioEnergiaP4'), datos.get('PrecioEnergiaP5'), datos.get('PrecioEnergiaP6')
        )

        cursor.execute(query, valores)
        conn.commit()
        print(f"✅ REGISTRO OK | Fecha: {datos.get('FechaFactura')} | Días: {datos.get('DiasFactura')} | {nombre_fichero}")
        conn.close()

    except Exception as e:
        print(f"❌ ERROR SQL en {nombre_fichero}: {e}")

# ==========================================
# 5. MAIN
# ==========================================
def main():
    try: client = DocumentIntelligenceClient(endpoint=ENDPOINT, credential=AzureKeyCredential(KEY))
    except Exception as e: 
        print(f"Error conexión: {e}")
        return

    os.system('cls' if os.name == 'nt' else 'clear')
    print("--- PROCESO FINAL ELEIA v5 (FECHAS FIX) ---")
    archivos = [f for f in os.listdir(CARPETA_FACTURAS) if f.endswith(".pdf")]
    
    for archivo in archivos:
        try:
            ruta = os.path.join(CARPETA_FACTURAS, archivo)
            datos = analizar_factura_custom(client, ruta)
            if datos: insertar_en_bd(datos, archivo)
        except Exception as ex: print(f"⚠️ Error {archivo}: {ex}")
    
    print("\n--- ¡FIN! ---")

if __name__ == "__main__":
    main()