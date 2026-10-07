"""Fur shell recipes, UV region matching and native geometry generation.

Recipes contain UV-triangle digests and scalar layer data, never source meshes,
rigs or vertex positions. Source vertex displacement/shading graphs are reused.
"""
import hashlib
import json
import math
import struct
from collections import Counter
from array import array
import bpy

API_REVISION = 524
VERSION = 1
SCALE = 100000
UV_ATTRIBUTE = 'CMB_FurUV1'
LAYER_ATTRIBUTE = 'CMB_FurLayer'
META = 'cmb_fur_regions'


def uv_digest(mesh, polygon, uv=None):
    from . import uv_transfer
    uv = uv or uv_transfer.primary_uv(mesh)
    if uv is None:return None
    points = sorted((round(float(uv.data[i].uv.x)*SCALE),
                     round(float(uv.data[i].uv.y)*SCALE)) for i in polygon.loop_indices)
    packed = struct.pack('<I',len(points)) + b''.join(struct.pack('<2q',*p) for p in points)
    return hashlib.blake2b(packed,digest_size=16).hexdigest()


def saved_regions(obj):
    try:return json.loads(obj.get(META,'[]'))
    except (TypeError,ValueError):return []


def extract(obj, carrier_name):
    """Recognize equal-topology, UV1-encoded, evenly offset imported shells."""
    from . import core
    mesh=obj.data
    fur_slots={i for i,m in enumerate(mesh.materials) if m and core.material_uber_part(m)=='Fur'}
    from . import uv_transfer
    if not fur_slots or not uv_transfer.primary_uv(mesh) or not mesh.uv_layers.get('UV1'):
        return None
    uv1=mesh.uv_layers['UV1']
    layers={}
    for p in mesh.polygons:
        values=[tuple(float(v) for v in uv1.data[i].uv) for i in p.loop_indices]
        if not values or any(abs(v[0]-values[0][0])>1e-6 or abs(v[1]-values[0][1])>1e-6 for v in values):
            return None
        key=tuple(round(v,6) for v in values[0])
        layers.setdefault(key,[]).append(p)
    ordered=sorted(layers)
    if not 2<=len(ordered)<=64 or ordered[0]!=(0.,0.):
        return None
    base_faces=layers[ordered[0]]
    base_slots={p.material_index for p in base_faces}
    shell_slots={p.material_index for k in ordered[1:] for p in layers[k]}
    if len(base_slots)!=1 or len(shell_slots)!=1 or not shell_slots.issubset(fur_slots):
        return None
    base_slot=next(iter(base_slots));shell_slot=next(iter(shell_slots))
    base_ids=sorted({v for p in base_faces for v in p.vertices})
    base_rank={v:i for i,v in enumerate(base_ids)}
    topology=[tuple(base_rank[v] for v in p.vertices) for p in base_faces]
    fingerprints=Counter(uv_digest(mesh,p) for p in base_faces)
    spacing=[0.]
    exact_uv=[list(uv1.data[base_faces[0].loop_start].uv)]
    for key in ordered[1:]:
        polys=layers[key]
        ids=sorted({v for p in polys for v in p.vertices})
        rank={v:i for i,v in enumerate(ids)}
        if (len(ids)!=len(base_ids) or [tuple(rank[v] for v in p.vertices) for p in polys]!=topology
                or Counter(uv_digest(mesh,p) for p in polys)!=fingerprints):
            return None
        distances=[]
        for a,b in zip(base_ids,ids):
            delta=mesh.vertices[b].co-mesh.vertices[a].co
            normal=mesh.vertices[a].normal.normalized()
            distance=delta.dot(normal)
            if (delta-normal*distance).length>1e-5:return None
            distances.append(distance)
        mean=sum(distances)/len(distances)
        if max(abs(d-mean) for d in distances)>1e-5:return None
        spacing.append(mean)
        exact_uv.append(list(uv1.data[polys[0].loop_start].uv))
    root_graphs=[m.node_group for m in obj.modifiers if m.type=='NODES' and m.node_group]
    if not any(any(n.type=='GROUP' and n.inputs.get('_FurLengthIntensity') is not None
                   and n.outputs.get('ret_positionWS') is not None for n in t.nodes) for t in root_graphs):
        return None
    family=obj.name.split('_fur_',1)[0] if '_fur_' in obj.name else ''
    identity=hashlib.sha256((obj.name+str(sorted(fingerprints.items()))).encode()).hexdigest()[:16]
    base_maps={core.normalized(value) for tree in core.walk_trees(mesh.materials[base_slot].node_tree)
               for node in tree.nodes if node.bl_idname=='ShaderNodeTexImage' and node.label=='_BaseMap' and node.image
               for value in (node.image.name,node.image.filepath) if value}
    return dict(version=VERSION,id=identity,carrier=carrier_name,source_object=obj.name,
                family=family,base_slot=base_slot,shell_slot=shell_slot,
                layers=exact_uv,spacing=spacing,faces=dict(fingerprints),
                base_faces=len(base_faces),base_vertices=len(base_ids),quantization=SCALE,
                base_map_keys=sorted(base_maps))


def validate(profile):
    if not isinstance(profile,dict) or profile.get('version')!=VERSION or profile.get('quantization')!=SCALE:
        raise RuntimeError('不支持的毛绒层配方，请重新导出材质包')
    layers=profile.get('layers',[]);spacing=profile.get('spacing',[])
    if (not 2<=len(layers)<=64 or len(layers)!=len(spacing)
            or any(not isinstance(v,(list,tuple)) or len(v)!=2 or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in v) for v in layers)
            or any(not isinstance(x,(int,float)) or not math.isfinite(x) or abs(x)>1 for x in spacing)):
        raise RuntimeError('毛绒层数或偏移数据无效')
    faces=profile.get('faces')
    if not isinstance(faces,dict) or not faces or len(faces)>200000:
        raise RuntimeError('毛绒区域指纹无效')
    if any(not isinstance(k,str) or len(k)!=32 or any(c not in '0123456789abcdef' for c in k)
           or type(v)!=int or not 0<v<=10000 for k,v in faces.items()):
        raise RuntimeError('毛绒区域指纹无效')
    if sum(faces.values())!=profile.get('base_faces'):
        raise RuntimeError('毛绒区域面数与指纹不一致')
    for k in ('base_slot','shell_slot'):
        if type(profile.get(k))!=int or profile[k]<0:raise RuntimeError('毛绒材质槽无效')
    for k in ('id','carrier','source_object'):
        if not isinstance(profile.get(k),str) or not profile[k]:raise RuntimeError('毛绒配方身份无效')
    return profile


def applicable(profile, entry):
    if entry.get('carrier')==profile['carrier']:return True
    family=profile.get('family','')
    same_family=not family or entry.get('source_object','').startswith(family+'_')
    return bool(same_family and set(profile.get('base_map_keys',())).intersection(entry.get('textures',())))


def region_indices(obj, recipe):
    """Require every source UV face and multiplicity; never use a fuzzy guess."""
    expected=Counter(recipe['faces'])
    matched=[p.index for p in obj.data.polygons if uv_digest(obj.data,p) in expected]
    actual=Counter(uv_digest(obj.data,obj.data.polygons[i]) for i in matched)
    if not matched:return []
    if actual!=expected:
        raise RuntimeError(obj.name+': 毛绒 UV 区域不完整或重复，无法安全重建壳层')
    return matched


def write_region(obj, recipe, indices):
    name='CMB_FurRegion_'+recipe['id']
    attr=obj.data.attributes.get(name)
    if attr and (attr.domain!='FACE' or attr.data_type!='BOOLEAN'):
        raise RuntimeError('毛绒区域属性同名冲突: '+name)
    if attr is None:attr=obj.data.attributes.new(name,'BOOLEAN','FACE')
    values=[False]*len(obj.data.polygons)
    for i in indices:values[i]=True
    attr.data.foreach_set('value',values)
    records=saved_regions(obj)
    if not any(r['id']==recipe['id'] for r in records):
        records.append({'id':recipe['id'],'faces':recipe['faces'],'quantization':SCALE,
                        'base_faces':recipe['base_faces']})
        obj[META]=json.dumps(records,separators=(',',':'))
    obj.data.update()
    return name


def restore_regions(obj, records):
    for recipe in records:
        if recipe.get('quantization')!=SCALE:raise RuntimeError('毛绒区域格式不支持')
        indices=region_indices(obj,recipe)
        if not indices:raise RuntimeError(obj.name+': 最终包毛绒区域与目标 UV 不匹配')
        write_region(obj,recipe,indices)


def _attribute(tree,name,dtype='FLOAT'):
    n=tree.nodes.new('GeometryNodeInputNamedAttribute')
    n.data_type=dtype;n.inputs['Name'].default_value=name
    return n.outputs['Attribute']


def build_generator(profile, mask_name, base_material, shell_material):
    """Duplicate a complete connected patch as instances; preserve UV/normal data."""
    tree=bpy.data.node_groups.new('CMB Fur Shells '+profile['id'],'GeometryNodeTree')
    tree['cmb_fur_shells_version']=VERSION
    for io in ('INPUT','OUTPUT'):
        tree.interface.new_socket(name='Geometry',in_out=io,socket_type='NodeSocketGeometry')
    nodes,links=tree.nodes,tree.links
    inp=nodes.new('NodeGroupInput');out=nodes.new('NodeGroupOutput')
    separate=nodes.new('GeometryNodeSeparateGeometry');separate.domain='FACE'
    links.new(inp.outputs['Geometry'],separate.inputs['Geometry'])
    links.new(_attribute(tree,mask_name,'BOOLEAN'),separate.inputs['Selection'])
    instance=nodes.new('GeometryNodeGeometryToInstance')
    links.new(separate.outputs['Selection'],instance.inputs['Geometry'])
    duplicate=nodes.new('GeometryNodeDuplicateElements');duplicate.domain='INSTANCE'
    duplicate.inputs['Amount'].default_value=len(profile['layers'])
    links.new(instance.outputs['Instances'],duplicate.inputs['Geometry'])
    def lookup(values):
        switch=nodes.new('GeometryNodeIndexSwitch');switch.data_type='FLOAT'
        switch.index_switch_items.clear()
        for v in values:switch.index_switch_items.new()
        for i,v in enumerate(values):switch.inputs[i+1].default_value=v
        links.new(duplicate.outputs['Duplicate Index'],switch.inputs['Index'])
        return switch.outputs[0]
    combine=nodes.new('ShaderNodeCombineXYZ')
    links.new(lookup([v[0] for v in profile['layers']]),combine.inputs['X'])
    links.new(lookup([v[1] for v in profile['layers']]),combine.inputs['Y'])
    uv=nodes.new('GeometryNodeStoreNamedAttribute');uv.domain='INSTANCE';uv.data_type='FLOAT_VECTOR'
    uv.inputs['Name'].default_value=UV_ATTRIBUTE
    links.new(duplicate.outputs['Geometry'],uv.inputs['Geometry']);links.new(combine.outputs[0],uv.inputs['Value'])
    bias=nodes.new('GeometryNodeStoreNamedAttribute');bias.domain='INSTANCE';bias.data_type='FLOAT'
    bias.inputs['Name'].default_value=LAYER_ATTRIBUTE
    links.new(uv.outputs['Geometry'],bias.inputs['Geometry']);links.new(lookup(profile['spacing']),bias.inputs['Value'])
    realize=nodes.new('GeometryNodeRealizeInstances')
    links.new(bias.outputs['Geometry'],realize.inputs['Geometry'])
    normal=nodes.new('GeometryNodeInputNormal')
    offset=nodes.new('ShaderNodeVectorMath');offset.operation='SCALE'
    links.new(normal.outputs[0],offset.inputs[0]);links.new(_attribute(tree,LAYER_ATTRIBUTE),offset.inputs['Scale'])
    position=nodes.new('GeometryNodeSetPosition')
    links.new(realize.outputs['Geometry'],position.inputs['Geometry']);links.new(offset.outputs[0],position.inputs['Offset'])
    material=nodes.new('GeometryNodeSetMaterial');material.inputs['Material'].default_value=shell_material
    links.new(position.outputs['Geometry'],material.inputs['Geometry'])
    split=nodes.new('ShaderNodeSeparateXYZ');links.new(_attribute(tree,UV_ATTRIBUTE,'FLOAT_VECTOR'),split.inputs[0])
    compare=nodes.new('ShaderNodeMath');compare.operation='COMPARE'
    compare.inputs[1].default_value=0.;compare.inputs[2].default_value=1e-7
    links.new(split.outputs['X'],compare.inputs[0])
    base=nodes.new('GeometryNodeSetMaterial');base.inputs['Material'].default_value=base_material
    links.new(material.outputs['Geometry'],base.inputs['Geometry']);links.new(compare.outputs[0],base.inputs['Selection'])
    join=nodes.new('GeometryNodeJoinGeometry')
    links.new(separate.outputs['Inverted'],join.inputs[0]);links.new(base.outputs['Geometry'],join.inputs[0])
    links.new(join.outputs['Geometry'],out.inputs['Geometry'])
    return tree


def adapt_shader(material):
    """The dynamically generated UV1 is a vector attribute, not a stored UV layer."""
    from . import core
    # Imported materials already belong to the transfer. Localize nested paths
    # before replacing a reader; other characters' shared shader math stays intact.
    cache={}
    def needs(tree,seen=None):
        seen=set() if seen is None else seen
        if tree in seen:return False
        seen.add(tree)
        return any(n.bl_idname=='ShaderNodeUVMap' and n.uv_map=='UV1' for n in tree.nodes) or any(
            needs(n.node_tree,seen) for n in tree.nodes if n.type=='GROUP' and n.node_tree)
    def adapt(tree):
        for n in list(tree.nodes):
            if n.bl_idname=='ShaderNodeUVMap' and n.uv_map=='UV1':
                a=tree.nodes.new('ShaderNodeAttribute');a.attribute_type='GEOMETRY';a.attribute_name=UV_ATTRIBUTE
                a.label='CMB Fur UV1';a.location=n.location
                targets=[l.to_socket for l in n.outputs['UV'].links]
                for socket in targets:tree.links.new(a.outputs['Vector'],socket)
                tree.nodes.remove(n)
            elif n.type=='GROUP' and n.node_tree and needs(n.node_tree):
                source=n.node_tree
                if source not in cache:
                    cache[source]=source.copy();cache[source].use_fake_user=False
                    adapt(cache[source])
                n.node_tree=cache[source]
    adapt(material.node_tree)


def _adapt_vertex(root, slots):
    # Only the source root's material-index filters and UV1 input change.
    for node in root.nodes:
        if node.bl_idname=='GeometryNodeInputNamedAttribute' and not node.inputs['Name'].is_linked:
            if node.inputs['Name'].default_value=='UV1':
                node.inputs['Name'].default_value=UV_ATTRIBUTE
            if node.inputs['Name'].default_value=='material_index':
                for link in list(node.outputs['Attribute'].links):
                    cmp=link.to_node
                    if cmp.bl_idname!='ShaderNodeMath' or cmp.operation!='COMPARE' or cmp.inputs[1].is_linked:
                        raise RuntimeError('毛绒材质槽筛选不是已核验的节点结构')
                    old=int(round(cmp.inputs[1].default_value))
                    if old not in slots:raise RuntimeError('毛绒材质槽筛选超出配方范围')
                    cmp.inputs[1].default_value=slots[old]


def attach(obj, carrier, profile, manifest, primary_entry=None, existing_profiles=()):
    from . import core,defaults
    validate(profile)
    if profile['id'] in existing_profiles:
        if (primary_entry and primary_entry['carrier'] == profile['carrier']
                and primary_entry['slot'] == profile['shell_slot']):
            from . import fur_layers
            return fur_layers.reuse(obj, carrier, profile)
        # The original MMD already supplies outer shells in another mapped slot.
        # Keep its base/cloth part; do not generate a second shell stack there.
        return None
    if any(m.type=='NODES' and m.node_group and m.node_group.name.startswith('CMB Fur Shells '+profile['id']) for m in obj.modifiers):
        return None
    indices=region_indices(obj,profile)
    if not indices:return None
    for existing in saved_regions(obj):
        if existing['id']!=profile['id'] and set(existing['faces']).intersection(profile['faces']):
            raise RuntimeError(obj.name+': 多个毛绒配方的 UV 区域重叠，未重复生成')
    mask=write_region(obj,profile,indices)
    slots={};materials={}
    for source_slot in (profile['base_slot'],profile['shell_slot']):
        material=carrier.data.materials[source_slot]
        entry=next((e for e in manifest['entries'] if e['carrier']==profile['carrier'] and e['slot']==source_slot),None)
        if material is None or entry is None:raise RuntimeError('毛绒配方缺少材质')
        if entry.get('param_col') is not None:material['ruri_param_col']=entry['param_col']
        material['ruri_uber_part']=entry['uber_part']
        defaults.bind(material,dict(entry,default_role='OTHER'))
        existing=next((i for i,m in enumerate(obj.data.materials) if m==material),None)
        if existing is None:
            existing=len(obj.data.materials);obj.data.materials.append(material)
        slots[source_slot]=existing;materials[source_slot]=material
    adapt_shader(materials[profile['shell_slot']])
    generator=obj.modifiers.new('CMB Fur Shells','NODES')
    generator.node_group=build_generator(profile,mask,materials[profile['base_slot']],materials[profile['shell_slot']])
    # Follow the recipient's native deformation, precede copied material stacks.
    first=next((i for i,m in enumerate(obj.modifiers) if m!=generator and m.type=='NODES' and m.name.startswith('CMB ')),len(obj.modifiers)-1)
    with bpy.context.temp_override(object=obj,active_object=obj):
        bpy.ops.object.modifier_move_to_index(modifier=generator.name,index=first)
    if primary_entry and primary_entry['carrier']==profile['carrier']:
        mods=[m for m in obj.modifiers if m.type=='NODES' and m!=generator and m.name.startswith('CMB ')]
    else:
        mods=core.copy_modifier_snapshots(carrier,obj)
    for m in mods:
        if m.type=='NODES':_adapt_vertex(m.node_group,slots)
    obj.update_tag(refresh={'DATA'})
    return {'object':obj.name,'matched_faces':len(indices),'layers':len(profile['layers']),
            'generated_shell_faces':len(indices)*(len(profile['layers'])-1),'source':profile['source_object']}
