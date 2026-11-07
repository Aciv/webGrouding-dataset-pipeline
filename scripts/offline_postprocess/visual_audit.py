"""Reproducible visual audit windows; draws source facts and derived eligibility without changing labels."""
from pathlib import Path
import json
from PIL import Image, ImageDraw, ImageFont


def render_windows(root, windows, output):
    """Render original-pixel xyxy windows as pixels/retained/excluded columns; close all raster buffers."""
    root, output = Path(root), Path(output)
    panel_width, row_height = 520, 820
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 13)
    with Image.new('RGB', (panel_width*3, row_height*len(windows)), 'white') as sheet:
        draw = ImageDraw.Draw(sheet)
        for row, window in enumerate(windows):
            observation_id, box = window['observation_id'], window['bbox']
            folder = root/'observations'/observation_id
            result = json.loads((folder/'result.json').read_text())
            q = result['quality']
            graph = json.loads((folder/'hierarchy.json').read_text())
            keep = [n for n in graph['nodes'] if n['detection_eligible']]
            excluded = [n for n in graph['nodes'] if n.get('source_detection_eligible') and not n['detection_eligible']]
            scale = min((panel_width-16)/(box[2]-box[0]), (row_height-105)/(box[3]-box[1]), 1.5)
            with Image.open(q['source_image']) as image:
                with image.crop(box) as view:
                    thumb = view.resize((round(view.width*scale), round(view.height*scale)))
            try:
                for col in range(3):
                    x0, y0 = col*panel_width+8, row*row_height+100
                    sheet.paste(thumb, (x0, y0))
                    title = ['pixels', f'kept: {len(keep)}', f'excluded: {len(excluded)}'][col]
                    draw.text((x0, row*row_height+5), observation_id, fill='black', font=font)
                    draw.text((x0, row*row_height+25), f'{title}; {q["status"]}; split GT={bool(result.get("split_label") and result["split_label"]["supervision_eligible"])}', fill='black', font=font)
                    draw.text((x0, row*row_height+45), f'window={box}', fill='black', font=font)
                    draw.text((x0, row*row_height+65), 'green=kept; orange=represented/generated', fill='black', font=font)
                    draw.text((x0, row*row_height+82), 'red=dropped; blue=core cut', fill='black', font=font)
                    for node in ([] if col == 0 else keep if col == 1 else excluded):
                        b = node['bbox']
                        if b[0] >= box[2] or b[2] <= box[0] or b[1] >= box[3] or b[3] <= box[1]:
                            continue
                        rect = [x0+(max(b[0], box[0])-box[0])*scale, y0+(max(b[1], box[1])-box[1])*scale,
                                x0+(min(b[2], box[2])-box[0])*scale, y0+(min(b[3], box[3])-box[1])*scale]
                        color = '#dc7900' if node.get('generated') or node.get('represented_by') else '#009030' if col == 1 else '#e00030'
                        draw.rectangle(rect, outline=color, width=2)
                    if col == 1 and result.get('split_label'):
                        for y in result['split_label']['cut_lines_original_px']:
                            if box[1] < y < box[3]:
                                draw.line([x0, y0+(y-box[1])*scale, x0+thumb.width, y0+(y-box[1])*scale], fill='#004dff', width=3)
            finally:
                thumb.close()
        output.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(output)


def render_comparison(before_root, after_root, windows, output):
    """Compare identical original-pixel windows across versions; withheld graphs are visibly audit-only."""
    before_root, after_root, output = Path(before_root), Path(after_root), Path(output)
    panel_width, row_height = 520, 820
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 13)
    with Image.new('RGB', (panel_width*3, row_height*len(windows)), 'white') as sheet:
        draw = ImageDraw.Draw(sheet)
        for row, window in enumerate(windows):
            oid, box = window['observation_id'], window['bbox']
            versions = []
            for root in (before_root, after_root):
                folder = root/'observations'/oid
                result = json.loads((folder/'result.json').read_text())
                graph = json.loads((folder/'hierarchy.json').read_text())
                versions.append((result, [n for n in graph['nodes'] if n['detection_eligible']]))
            scale = min((panel_width-16)/(box[2]-box[0]), (row_height-110)/(box[3]-box[1]), 1.5)
            with Image.open(versions[1][0]['quality']['source_image']) as image:
                with image.crop(box) as view:
                    thumbnail = view.resize((round(view.width*scale), round(view.height*scale)))
            try:
                for col in range(3):
                    left, top = col*panel_width+8, row*row_height+105
                    sheet.paste(thumbnail, (left, top))
                    draw.text((left, row*row_height+5), oid, fill='black', font=font)
                    draw.text((left, row*row_height+25), f'window={box}', fill='black', font=font)
                    if col == 0:
                        draw.text((left, row*row_height+45), 'original pixels', fill='black', font=font)
                        continue
                    result, targets = versions[col-1]
                    q = result['quality']
                    label = result.get('split_label')
                    grade = q.get('evidence', {}).get('boundary_supervision', {})
                    name = 'v1' if col == 1 else 'v2'
                    draw.text((left, row*row_height+45), f'{name}: {q["status"]}, targets={len(targets)}', fill='black', font=font)
                    draw.text((left, row*row_height+65), f'cut GT={bool(label and label["supervision_eligible"])}; complete-view loss={len(q.get("evidence",{}).get("unrepresented_target_ids",[]))}', fill='black', font=font)
                    draw.text((left, row*row_height+85), 'green=target; orange=generated; gray=WITHHELD' if q['status'] == 'accepted' else 'WITHHELD: source candidates shown for audit', fill='black', font=font)
                    for node in targets:
                        b = node['bbox']
                        if b[0] >= box[2] or b[2] <= box[0] or b[1] >= box[3] or b[3] <= box[1]:
                            continue
                        rect = [left+(max(b[0], box[0])-box[0])*scale, top+(max(b[1], box[1])-box[1])*scale,
                                left+(min(b[2], box[2])-box[0])*scale, top+(min(b[3], box[3])-box[1])*scale]
                        color = '#909090' if q['status'] != 'accepted' else '#d08000' if node.get('generated') else '#009030'
                        draw.rectangle(rect, outline=color, width=2)
                    if label:
                        for y in label['cut_lines_original_px']:
                            if box[1] < y < box[3]:
                                draw.line([left, top+(y-box[1])*scale, left+thumbnail.width, top+(y-box[1])*scale], fill='#004dff', width=3)
            finally:
                thumbnail.close()
        output.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(output)


def render_crop_boundary(root, observation_id, bbox, output):
    """Show actual adjacent crop windows with positives and ignores in global xyxy; unavailable pixels are gray."""
    root, output = Path(root), Path(output)
    result = json.loads((root/'observations'/observation_id/'result.json').read_text())
    crops = [c for c in result['crops'] if c['crop_bbox'][1] < bbox[3] and c['crop_bbox'][3] > bbox[1]][:3]
    panel_width, panel_height = 520, 850
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 13)
    scale = min((panel_width-16)/(bbox[2]-bbox[0]), (panel_height-100)/(bbox[3]-bbox[1]), 1.5)
    with Image.new('RGB', (panel_width*len(crops), panel_height), '#dddddd') as sheet:
        draw = ImageDraw.Draw(sheet)
        with Image.open(result['quality']['source_image']) as image:
            for pos, crop in enumerate(crops):
                x0, y0 = pos*panel_width+8, 90
                b = crop['crop_bbox']
                top, bottom = max(bbox[1], b[1]), min(bbox[3], b[3])
                with image.crop([bbox[0], top, bbox[2], bottom]) as patch:
                    with patch.resize((round(patch.width*scale), round(patch.height*scale))) as thumb:
                        sheet.paste(thumb, (x0, round(y0+(top-bbox[1])*scale)))
                draw.text((x0, 5), observation_id, fill='black', font=font)
                draw.text((x0, 25), f'{crop["crop_id"].rsplit("/",1)[1]} crop={b}', fill='black', font=font)
                draw.text((x0, 45), f'positives={len(crop["targets"])} ignores={len(crop["ignore_regions"])}', fill='black', font=font)
                draw.text((x0, 65), 'green=positive; magenta=ignore; blue=core', fill='black', font=font)
                for collection, color in [('ignore_regions', '#d000b0'), ('targets', '#008030')]:
                    for node in crop[collection]:
                        local = node['bbox']
                        box = [local[0], local[1]+b[1], local[2], local[3]+b[1]]
                        if box[0] >= bbox[2] or box[2] <= bbox[0] or box[1] >= bbox[3] or box[3] <= bbox[1]:
                            continue
                        draw.rectangle([x0+(max(box[0],bbox[0])-bbox[0])*scale, y0+(max(box[1],bbox[1])-bbox[1])*scale,
                                        x0+(min(box[2],bbox[2])-bbox[0])*scale, y0+(min(box[3],bbox[3])-bbox[1])*scale], outline=color, width=2)
                for y in (crop['core_bbox'][1], crop['core_bbox'][3]):
                    if bbox[1] < y < bbox[3]:
                        draw.line([x0,y0+(y-bbox[1])*scale,x0+(bbox[2]-bbox[0])*scale,y0+(y-bbox[1])*scale],fill='#004dff',width=3)
        output.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(output)
