import struct
R=['zero','at','v0','v1','a0','a1','a2','a3','t0','t1','t2','t3','t4','t5','t6','t7',
   's0','s1','s2','s3','s4','s5','s6','s7','t8','t9','k0','k1','gp','sp','fp','ra']
def s16(x): return x-0x10000 if x&0x8000 else x
def dis(w,pc):
    op=w>>26; rs=(w>>21)&31; rt=(w>>16)&31; rd=(w>>11)&31; sh=(w>>6)&31; fn=w&63; im=w&0xffff
    g=lambda i:'$'+R[i]
    if w==0: return 'nop'
    if op==0:
        t={0:'sll',2:'srl',3:'sra'}
        if fn in t: return f'{t[fn]} {g(rd)},{g(rt)},{sh}'
        t={4:'sllv',6:'srlv',7:'srav'}
        if fn in t: return f'{t[fn]} {g(rd)},{g(rt)},{g(rs)}'
        if fn==8: return f'jr {g(rs)}'
        if fn==9: return f'jalr {g(rd)},{g(rs)}'
        if fn==12: return 'syscall'
        if fn==13: return 'break'
        if fn in(16,18): return f"{'mfhi' if fn==16 else 'mflo'} {g(rd)}"
        if fn in(17,19): return f"{'mthi' if fn==17 else 'mtlo'} {g(rs)}"
        t={24:'mult',25:'multu',26:'div',27:'divu'}
        if fn in t: return (f'{t[fn]} {g(rd)},{g(rs)},{g(rt)}' if rd and fn in (24,25) else f'{t[fn]} {g(rs)},{g(rt)}')
        t={32:'add',33:'addu',34:'sub',35:'subu',36:'and',37:'or',38:'xor',39:'nor',42:'slt',43:'sltu'}
        if fn in t: return f'{t[fn]} {g(rd)},{g(rs)},{g(rt)}'
    if op==1:
        n={0:'bltz',1:'bgez',16:'bltzal',17:'bgezal'}.get(rt)
        if n: return f'{n} {g(rs)},0x{pc+4+(s16(im)<<2):x}'
    if op==2: return f'j 0x{((pc+4)&0xf0000000)|((w&0x3ffffff)<<2):x}'
    if op==3: return f'jal 0x{((pc+4)&0xf0000000)|((w&0x3ffffff)<<2):x}'
    if op in(4,5): return f"{'beq' if op==4 else 'bne'} {g(rs)},{g(rt)},0x{pc+4+(s16(im)<<2):x}"
    if op in(6,7): return f"{'blez' if op==6 else 'bgtz'} {g(rs)},0x{pc+4+(s16(im)<<2):x}"
    t={8:'addi',9:'addiu',10:'slti',11:'sltiu'}
    if op in t: return f'{t[op]} {g(rt)},{g(rs)},{s16(im)}'
    t={12:'andi',13:'ori',14:'xori'}
    if op in t: return f'{t[op]} {g(rt)},{g(rs)},0x{im:x}'
    if op==15: return f'lui {g(rt)},0x{im:x}'
    if op==16:
        if rs==0: return f'mfc0 {g(rt)},$cp0_{rd}'
        if rs==4: return f'mtc0 {g(rt)},$cp0_{rd}'
        if rs==16 and fn==16: return 'rfe'
    t={32:'lb',33:'lh',34:'lwl',35:'lw',36:'lbu',37:'lhu',38:'lwr',40:'sb',41:'sh',42:'swl',43:'sw',46:'swr'}
    if op in t: return f'{t[op]} {g(rt)},{s16(im)}({g(rs)})'
    return f'.word 0x{w:08x}'
def disasm(buf,base,start,end):
    for a in range(start,end,4):
        w=struct.unpack_from('<I',buf,a)[0]
        yield base+a,w,dis(w,base+a)
if __name__=='__main__':
    import sys
    buf=open(sys.argv[1],'rb').read(); s=int(sys.argv[2],0); e=int(sys.argv[3],0)
    for pc,w,t in disasm(buf,0x08000000,s,e): print(f'{pc:08x}: {w:08x}  {t}')
