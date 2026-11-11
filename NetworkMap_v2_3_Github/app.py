from flask import Flask, render_template, request, redirect, url_for, send_file
from pathlib import Path
from datetime import datetime
import html
import json
import re
import tempfile

import folium
import pandas as pd
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FOLDER = BASE_DIR / "config"
UPLOAD_FOLDER = BASE_DIR / "uploads"
MAP_FOLDER = BASE_DIR / "maps"
ALLOWED_EXTENSIONS = {".xlsx", ".xls"}
HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")


def load_json_config(filename):
    path = CONFIG_FOLDER / filename
    with path.open("r", encoding="utf-8") as config_file:
        data = json.load(config_file)
    if "_default" not in data:
        raise ValueError(f"{filename} must contain a _default entry")
    return data


def save_json_config(filename, data):
    """Write JSON atomically so an interrupted save cannot corrupt the config."""
    CONFIG_FOLDER.mkdir(exist_ok=True)
    destination = CONFIG_FOLDER / filename
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=CONFIG_FOLDER, delete=False, suffix=".tmp"
    ) as temporary_file:
        json.dump(data, temporary_file, ensure_ascii=False, indent=2)
        temporary_file.write("\n")
        temporary_path = Path(temporary_file.name)
    temporary_path.replace(destination)


def clean_text(value, fallback="Unspecified"):
    if pd.isna(value):
        return fallback
    text = str(value).strip()
    return text if text else fallback


def normalise_header(value):
    return "".join(character.lower() for character in str(value) if character.isalnum())


COLUMN_HINTS = {
    "start_name": ["startname", "startsite", "fromsite", "aname", "sitea"],
    "start_lat": ["startlat", "startlatitude", "fromlat", "alatitude", "lata"],
    "start_lon": ["startlon", "startlng", "startlongitude", "fromlon", "alongitude", "lona"],
    "end_name": ["endname", "endsite", "tosite", "bname", "siteb"],
    "end_lat": ["endlat", "endlatitude", "tolat", "blatitude", "latb"],
    "end_lon": ["endlon", "endlng", "endlongitude", "tolon", "blongitude", "lonb"],
    "category": ["category", "categories", "class", "classification", "priority", "prio", "type"],
    "project": ["project", "projects", "programme", "program", "initiative", "status"],
}


def suggest_columns(columns):
    normalised = {normalise_header(column): column for column in columns}
    suggestions = {}
    for target, aliases in COLUMN_HINTS.items():
        if target in normalised:
            suggestions[target] = normalised[target]
            continue
        for alias in aliases:
            if alias in normalised:
                suggestions[target] = normalised[alias]
                break
    return suggestions


CATEGORY_CONFIG = {}
PROJECT_CONFIG = {}


def reload_configs():
    global CATEGORY_CONFIG, PROJECT_CONFIG
    CATEGORY_CONFIG = load_json_config("categories.json")
    PROJECT_CONFIG = load_json_config("projects.json")


reload_configs()


def category_style(value):
    configured = CATEGORY_CONFIG.get(value, CATEGORY_CONFIG["_default"])
    return {
        "color": configured.get("color", "#000000"),
        "weight": float(configured.get("weight", 2)),
    }


def project_style(value):
    configured = PROJECT_CONFIG.get(value, PROJECT_CONFIG["_default"])
    return {
        "color": configured.get("marker_color", configured.get("color", "#000000")),
        "size": float(configured.get("marker_size", 5)),
        "weight": float(configured.get("line_weight", 4)),
    }


def get_line_style(value, source):
    if source == "project":
        style = project_style(value)
        return {"color": style["color"], "weight": style["weight"]}
    return category_style(value)


def get_marker_style(value, source):
    if source == "category":
        style = category_style(value)
        return {
            "color": style["color"],
            "size": max(4, min(12, style["weight"] + 2)),
        }
    style = project_style(value)
    return {"color": style["color"], "size": style["size"]}


def build_working_dataframe(source_df, form):
    required_fields = [
        "start_name", "start_lat", "start_lon",
        "end_name", "end_lat", "end_lon",
        "category", "project",
    ]

    missing_form_fields = [field for field in required_fields if not form.get(field)]
    if missing_form_fields:
        raise ValueError("Missing column selections: " + ", ".join(missing_form_fields))

    missing_columns = [form[field] for field in required_fields if form[field] not in source_df.columns]
    if missing_columns:
        raise ValueError("Selected columns were not found: " + ", ".join(missing_columns))

    df = pd.DataFrame({field: source_df[form[field]] for field in required_fields})

    for coordinate in ["start_lat", "start_lon", "end_lat", "end_lon"]:
        df[coordinate] = pd.to_numeric(df[coordinate], errors="coerce")

    valid_coordinates = (
        df["start_lat"].between(-90, 90)
        & df["end_lat"].between(-90, 90)
        & df["start_lon"].between(-180, 180)
        & df["end_lon"].between(-180, 180)
    )
    df = df.loc[valid_coordinates].copy()

    if df.empty:
        raise ValueError("No rows contain a complete, valid set of start and end coordinates.")

    for field in ["start_name", "end_name", "category", "project"]:
        df[field] = df[field].map(clean_text)

    return df


def create_map_with_lines(df, marker_source="project", line_source="category", filter_source="category"):
    valid_sources = {"category", "project"}
    if marker_source not in valid_sources or line_source not in valid_sources or filter_source not in valid_sources:
        raise ValueError("Visualisation sources must be category or project.")

    center_lat = pd.concat([df["start_lat"], df["end_lat"]]).mean()
    center_lon = pd.concat([df["start_lon"], df["end_lon"]]).mean()

    map_object = folium.Map(location=[center_lat, center_lon], zoom_start=7, tiles=None)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles &copy; Esri &mdash; Source: Esri and contributors",
        name="Esri World Street Map",
        overlay=False,
        control=True,
    ).add_to(map_object)

    layer_groups = {}
    start_locations = []
    marker_values = set()
    line_values = set()

    for _, row in df.iterrows():
        category_value = clean_text(row["category"])
        project_value = clean_text(row["project"])
        values = {"category": category_value, "project": project_value}

        marker_value = values[marker_source]
        line_value = values[line_source]
        layer_value = values[filter_source]
        marker_values.add(marker_value)
        line_values.add(line_value)

        if layer_value not in layer_groups:
            layer_groups[layer_value] = folium.FeatureGroup(name=layer_value, show=True)
            layer_groups[layer_value].add_to(map_object)
        group = layer_groups[layer_value]

        marker = get_marker_style(marker_value, marker_source)
        line = get_line_style(line_value, line_source)

        popup = (
            f"<b>{html.escape(row['start_name'])}</b>"
            f"<br>End: {html.escape(row['end_name'])}"
            f"<br>Category: {html.escape(category_value)}"
            f"<br>Project: {html.escape(project_value)}"
        )

        folium.CircleMarker(
            location=[row["start_lat"], row["start_lon"]],
            popup=popup,
            radius=marker["size"],
            color=marker["color"],
            fill=True,
            fill_color=marker["color"],
            fill_opacity=0.9,
            weight=2,
        ).add_to(group)

        folium.CircleMarker(
            location=[row["end_lat"], row["end_lon"]],
            popup=(
                f"<b>{html.escape(row['end_name'])}</b>"
                f"<br>Category: {html.escape(category_value)}"
                f"<br>Project: {html.escape(project_value)}"
            ),
            radius=3,
            color="#d32f2f",
            fill=True,
            fill_color="#d32f2f",
            fill_opacity=0.8,
            weight=1,
        ).add_to(group)

        folium.PolyLine(
            locations=[
                [row["start_lat"], row["start_lon"]],
                [row["end_lat"], row["end_lon"]],
            ],
            color=line["color"],
            weight=line["weight"],
            opacity=0.8,
            popup=f"{html.escape(line_source.title())}: {html.escape(line_value)}",
        ).add_to(group)

        start_locations.append({
            "name": row["start_name"],
            "lat": float(row["start_lat"]),
            "lon": float(row["start_lon"]),
            "category": category_value,
            "project": project_value,
        })

    marker_legend = "".join(
        f'<div><span style="color:{get_marker_style(value, marker_source)["color"]};font-size:20px;">&#9679;</span> {html.escape(value)}</div>'
        for value in sorted(marker_values)
    )
    line_legend = "".join(
        f'<div><span style="color:{get_line_style(value, line_source)["color"]};">&#9644;&#9644;&#9644;</span> {html.escape(value)}</div>'
        for value in sorted(line_values)
    )

    legend_html = f"""
    <div style="position:fixed;bottom:35px;left:35px;max-height:55vh;overflow:auto;
                background:white;padding:12px;border:2px solid #777;border-radius:5px;
                z-index:1000;box-shadow:0 2px 6px rgba(0,0,0,.25);font-size:13px;">
        <h4 style="margin:0 0 6px;">Markers by {html.escape(marker_source.title())}</h4>
        {marker_legend}
        <hr>
        <h4 style="margin:0 0 6px;">Lines by {html.escape(line_source.title())}</h4>
        {line_legend}
        <hr>
        <div>Layers filter by {html.escape(filter_source.title())}</div>
    </div>
    """
    map_object.get_root().html.add_child(folium.Element(legend_html))

    search_data = json.dumps(start_locations, ensure_ascii=False).replace("</", "<\\/")
    search_html = f"""
    <div id="search-box" style="position:fixed;top:80px;right:10px;background:white;padding:10px;
         border:2px solid #777;z-index:1000;width:260px;border-radius:5px;box-shadow:0 2px 6px rgba(0,0,0,.25);">
        <strong>Search start location</strong>
        <input id="location-search" type="text" placeholder="Enter a name..."
               style="box-sizing:border-box;width:100%;padding:8px;margin-top:8px;">
        <div id="search-results" style="display:none;max-height:220px;overflow:auto;border:1px solid #ddd;"></div>
        <button id="clear-highlight" type="button" style="display:none;width:100%;margin-top:6px;">Clear highlight</button>
    </div>
    <script>
    const startLocations = {search_data};
    let mapObj = null;
    let highlightMarker = null;
    let highlightCircle = null;

    function findMap() {{
        for (const key in window) {{
            const candidate = window[key];
            if (candidate && candidate._container && candidate._container.classList && candidate._container.classList.contains('folium-map')) {{
                mapObj = candidate;
                return;
            }}
        }}
    }}

    function clearHighlight() {{
        if (mapObj && highlightMarker) mapObj.removeLayer(highlightMarker);
        if (mapObj && highlightCircle) mapObj.removeLayer(highlightCircle);
        highlightMarker = null;
        highlightCircle = null;
        document.getElementById('clear-highlight').style.display = 'none';
    }}

    function highlightLocation(location) {{
        if (!mapObj) findMap();
        if (!mapObj) return;
        clearHighlight();
        highlightCircle = L.circle([location.lat, location.lon], {{color:'#FFD700',fillColor:'#FFD700',fillOpacity:.22,radius:500,weight:3}}).addTo(mapObj);
        highlightMarker = L.circleMarker([location.lat, location.lon], {{radius:10,color:'#FFD700',fillColor:'#FFD700',fillOpacity:.85,weight:3}}).addTo(mapObj);
        highlightMarker.bindPopup('<b>' + location.name + '</b><br>Category: ' + location.category + '<br>Project: ' + location.project).openPopup();
        mapObj.setView([location.lat, location.lon], 12);
        document.getElementById('clear-highlight').style.display = 'block';
    }}

    document.addEventListener('DOMContentLoaded', function() {{
        findMap();
        const input = document.getElementById('location-search');
        const results = document.getElementById('search-results');
        input.addEventListener('input', function() {{
            const term = input.value.toLowerCase().trim();
            results.innerHTML = '';
            if (term.length < 2) {{ results.style.display = 'none'; return; }}
            const matches = startLocations.filter(item => item.name.toLowerCase().includes(term)).slice(0, 15);
            if (!matches.length) results.innerHTML = '<div style="padding:8px;color:#777;">No matches</div>';
            matches.forEach(function(item) {{
                const option = document.createElement('div');
                option.textContent = item.name;
                option.style.cssText = 'padding:8px;cursor:pointer;border-bottom:1px solid #eee;';
                option.onclick = function() {{ input.value = item.name; results.style.display = 'none'; highlightLocation(item); }};
                results.appendChild(option);
            }});
            results.style.display = 'block';
        }});
        document.getElementById('clear-highlight').addEventListener('click', function() {{ clearHighlight(); input.value = ''; }});
    }});
    </script>
    """
    map_object.get_root().html.add_child(folium.Element(search_html))

    folium.LayerControl(collapsed=False).add_to(map_object)
    map_object.fit_bounds([
        [min(df["start_lat"].min(), df["end_lat"].min()), min(df["start_lon"].min(), df["end_lon"].min())],
        [max(df["start_lat"].max(), df["end_lat"].max()), max(df["start_lon"].max(), df["end_lon"].max())],
    ])
    return map_object


app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["MAP_FOLDER"] = str(MAP_FOLDER)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
UPLOAD_FOLDER.mkdir(exist_ok=True)
MAP_FOLDER.mkdir(exist_ok=True)


def render_style_manager(message=None, error=None):
    reload_configs()
    categories = {name: value for name, value in CATEGORY_CONFIG.items() if name != "_default"}
    projects = {name: value for name, value in PROJECT_CONFIG.items() if name != "_default"}
    return render_template(
        "styles.html", categories=categories, projects=projects,
        category_default=CATEGORY_CONFIG["_default"], project_default=PROJECT_CONFIG["_default"],
        message=message, error=error,
    )


def validate_style_name(name):
    name = clean_text(name, "")
    if not name:
        raise ValueError("A style name is required.")
    if name == "_default":
        raise ValueError("The reserved name _default cannot be changed.")
    if len(name) > 100:
        raise ValueError("Style names may contain at most 100 characters.")
    return name


def validate_color(color):
    if not color or not HEX_COLOR_PATTERN.fullmatch(color):
        raise ValueError("Choose a valid colour.")
    return color.upper()


def validate_number(value, label, minimum, maximum):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.")
    if not minimum <= number <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return int(number) if number.is_integer() else number


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        return render_template("index.html")

    uploaded_file = request.files.get("file")
    if uploaded_file is None:
        return render_template("index.html", error="No file was included in the request.")
    if uploaded_file.filename == "":
        return render_template("index.html", error="Select an Excel file first.")

    filename = secure_filename(uploaded_file.filename)
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        return render_template("index.html", error="Only .xlsx and .xls files are supported.")

    file_path = UPLOAD_FOLDER / filename
    try:
        uploaded_file.save(file_path)
        dataframe = pd.read_excel(file_path)
        if dataframe.empty:
            raise ValueError("The selected worksheet contains no data rows.")
        columns = [str(column) for column in dataframe.columns]
        return render_template(
            "mapping.html",
            columns=columns,
            filename=filename,
            suggested=suggest_columns(columns),
        )
    except Exception as error:
        return render_template("index.html", error=str(error))


@app.route("/styles")
def styles():
    return render_style_manager(message=request.args.get("message"))


@app.route("/styles/category/save", methods=["POST"])
def save_category_style():
    try:
        original_name = request.form.get("original_name", "").strip()
        name = validate_style_name(request.form.get("name"))
        color = validate_color(request.form.get("color"))
        weight = validate_number(request.form.get("weight"), "Weight", 1, 20)
        config = load_json_config("categories.json")
        if original_name and original_name != name:
            if original_name == "_default":
                raise ValueError("The default style cannot be renamed.")
            config.pop(original_name, None)
        config[name] = {"color": color, "weight": weight}
        save_json_config("categories.json", config)
        reload_configs()
        return redirect(url_for("styles", message=f'Category style "{name}" saved.'))
    except Exception as error:
        return render_style_manager(error=str(error)), 400


@app.route("/styles/project/save", methods=["POST"])
def save_project_style():
    try:
        original_name = request.form.get("original_name", "").strip()
        name = validate_style_name(request.form.get("name"))
        color = validate_color(request.form.get("color"))
        marker_size = validate_number(request.form.get("marker_size"), "Marker size", 1, 30)
        config = load_json_config("projects.json")
        if original_name and original_name != name:
            if original_name == "_default":
                raise ValueError("The default style cannot be renamed.")
            config.pop(original_name, None)
        config[name] = {"marker_color": color, "marker_size": marker_size}
        save_json_config("projects.json", config)
        reload_configs()
        return redirect(url_for("styles", message=f'Project style "{name}" saved.'))
    except Exception as error:
        return render_style_manager(error=str(error)), 400


@app.route("/styles/category/delete", methods=["POST"])
def delete_category_style():
    try:
        name = validate_style_name(request.form.get("delete_name"))
        config = load_json_config("categories.json")
        if name not in config:
            raise ValueError(f'Category style "{name}" was not found.')
        del config[name]
        save_json_config("categories.json", config)
        reload_configs()
        return redirect(url_for("styles", message=f'Category style "{name}" deleted.'))
    except Exception as error:
        return render_style_manager(error=str(error)), 400


@app.route("/styles/project/delete", methods=["POST"])
def delete_project_style():
    try:
        name = validate_style_name(request.form.get("delete_name"))
        config = load_json_config("projects.json")
        if name not in config:
            raise ValueError(f'Project style "{name}" was not found.')
        del config[name]
        save_json_config("projects.json", config)
        reload_configs()
        return redirect(url_for("styles", message=f'Project style "{name}" deleted.'))
    except Exception as error:
        return render_style_manager(error=str(error)), 400


@app.route("/generate_map", methods=["POST"])
def generate_map():
    filename = secure_filename(request.form.get("filename", ""))
    file_path = UPLOAD_FOLDER / filename
    if not filename or not file_path.exists():
        return render_template("index.html", error="The uploaded file could not be found. Upload it again.")

    try:
        reload_configs()
        source_df = pd.read_excel(file_path)
        df = build_working_dataframe(source_df, request.form)
        marker_source = request.form.get("marker_source", "project")
        line_source = request.form.get("line_source", "category")
        filter_source = request.form.get("filter_source", "category")

        map_result = create_map_with_lines(
            df,
            marker_source=marker_source,
            line_source=line_source,
            filter_source=filter_source,
        )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        stem = Path(filename).stem
        map_filename = f"map_{stem}_{timestamp}.html"
        map_path = MAP_FOLDER / map_filename
        map_result.save(map_path)
        return redirect(url_for("view_map", filename=map_filename))
    except Exception as error:
        columns = [str(column) for column in pd.read_excel(file_path, nrows=0).columns]
        return render_template(
            "mapping.html",
            columns=columns,
            filename=filename,
            suggested=suggest_columns(columns),
            error=str(error),
        ), 400


@app.route("/view_map/<path:filename>")
def view_map(filename):
    safe_filename = secure_filename(filename)
    response = send_file(MAP_FOLDER / safe_filename)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/download_map/<path:filename>")
def download_map(filename):
    safe_filename = secure_filename(filename)
    return send_file(MAP_FOLDER / safe_filename, as_attachment=True, download_name=safe_filename)


if __name__ == "__main__":
    app.run(debug=True)
