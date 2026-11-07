"""Small real-data contact sheets, separate from fixture tests and label generation."""
from pathlib import Path
import json
from PIL import Image, ImageDraw
from .infrastructure import read_jsonl


def render_contact_sheet(root, observation_ids, output):
    """Render raw/retained target overlays on original-pixel windows; close each source image immediately."""
    root, output = Path(root), Path(output)
    row_height, panel_width = 470, 300
    with Image.new('RGB', (panel_width*3, row_height*len(observation_ids)), 'white') as sheet:
        drawing = ImageDraw.Draw(sheet)
        for row, observation_id in enumerate(observation_ids):
            result = json.loads((root/'observations'/observation_id/'result.json').read_text())
            q = result['quality']
            source = json.loads(Path(q['source_hierarchy']).read_text())
            graph_path = root/'observations'/observation_id/'hierarchy.json'
            graph = json.loads(graph_path.read_text()) if graph_path.exists() else {'nodes': []}
            width, height = source['capture']['width'], source['capture']['height']
            source_boxes = [[n['bbox'][0], n['bbox'][1], n['bbox'][0]+n['bbox'][2], n['bbox'][1]+n['bbox'][3]]
                            for n in source['nodes'] if n.get('disposition') == 'detection']
            final = [n for n in graph['nodes'] if n['detection_eligible']]
            window_height = min(height, 1200)
            best_top = max(range(0, max(1, height-window_height+1), 100),
                           key=lambda top: sum(top <= (b[1]+b[3])/2 < top+window_height for b in source_boxes))
            scale = min((panel_width-12)/width, (row_height-70)/window_height)
            with Image.open(q['source_image']) as image:
                with image.crop([0, best_top, width, best_top+window_height]) as window:
                    thumb = window.resize((round(width*scale), round(window_height*scale)))
            try:
                for column in range(3):
                    left, top = column*panel_width+5, row*row_height+62
                    sheet.paste(thumb, (left, top))
                    title = ('pixels', f'source: {len(source_boxes)}', f"clean: {len(final)} [{q['status']}]")[column]
                    drawing.text((left, row*row_height+5), observation_id, fill='black')
                    drawing.text((left, row*row_height+22), title, fill='black')
                    drawing.text((left, row*row_height+39), f'y={best_top}..{best_top+window_height}', fill='black')
                    boxes = [] if column == 0 else source_boxes if column == 1 else [n['bbox'] for n in final]
                    for box in boxes:
                        if box[3] <= best_top or box[1] >= best_top+window_height:
                            continue
                        rect = [left+box[0]*scale, top+max(0, box[1]-best_top)*scale,
                                left+box[2]*scale, top+min(window_height, box[3]-best_top)*scale]
                        drawing.rectangle(rect, outline='#d02020' if column == 1 else '#008020', width=1)
                    if column == 2 and result.get('split_label'):
                        for y in result['split_label']['cut_lines_original_px']:
                            if best_top < y < best_top+window_height:
                                drawing.line([left, top+(y-best_top)*scale, left+width*scale, top+(y-best_top)*scale], fill='#0055ff', width=2)
            finally:
                thumb.close()
        output.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(output)
